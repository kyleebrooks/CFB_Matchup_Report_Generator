"""Live feeds: breaking news and injury reports, pulled on a schedule.

Each feed is a recurring, league-wide research pull — the same live-web research
machinery the reports use, pointed at all of FBS instead of one matchup — whose
findings land in a durable, deduplicated table the site reads. The site shows
the newest items; the table keeps a real history behind them.

The scheduler follows schedules.py's pattern exactly: a daemon thread ticks
every minute, and a database claim (an UPDATE guarded by the previous
last_run_at) decides which worker fires, so multiple gunicorn workers never
double-pull. Settings — on/off, interval, research model, search engine — live
in the feed_state table and are edited from the cfbreports console through the
API.
"""

import hashlib
import logging
import threading
import time
from datetime import datetime

import config
import db
import openrouter
import research

ITEMS_TABLE = 'feed_items'
STATE_TABLE = 'feed_state'
MAX_ITEMS_PER_FEED = 500          # the accessible history; older rows are trimmed
MIN_INTERVAL_MIN = 30
MAX_INTERVAL_MIN = 24 * 60

_ITEMS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    feed        VARCHAR(16)  NOT NULL,
    headline    VARCHAR(300) NOT NULL,
    detail      TEXT,
    team        VARCHAR(80)  DEFAULT NULL,
    player      VARCHAR(80)  DEFAULT NULL,
    position    VARCHAR(12)  DEFAULT NULL,
    status      VARCHAR(60)  DEFAULT NULL,
    impact      VARCHAR(300) DEFAULT NULL,
    source_name VARCHAR(120) DEFAULT NULL,
    source_url  VARCHAR(500) DEFAULT NULL,
    published   VARCHAR(60)  DEFAULT NULL,
    confidence  VARCHAR(12)  DEFAULT NULL,
    item_key    VARCHAR(40)  NOT NULL,
    created_at  DATETIME     NOT NULL,
    KEY idx_feed_id (feed, id),
    UNIQUE KEY uniq_feed_item (feed, item_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_STATE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
    feed             VARCHAR(16)  PRIMARY KEY,
    enabled          TINYINT      NOT NULL DEFAULT 1,
    interval_minutes INT          NOT NULL DEFAULT 180,
    research_model   VARCHAR(120) NOT NULL DEFAULT '',
    search_engine    VARCHAR(12)  NOT NULL DEFAULT '',
    last_run_at      DATETIME     DEFAULT NULL,
    last_status      VARCHAR(250) NOT NULL DEFAULT '',
    last_new_items   INT          NOT NULL DEFAULT 0,
    updated_at       DATETIME     DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class FeedError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# What each feed asks the research model for. The window stays short — pulls
# repeat, so each one only needs what is new since roughly the last one.
FEEDS = {
    'news': {
        'title': 'Breaking News',
        'interval_minutes': 180,
        'job': {
            'key': 'feed_news', 'scope': 'league', 'topic': 'news',
            'section': 'Breaking News', 'window': 2,
            'focus': ('the latest breaking college football news across the '
                      'FBS: coaching hires and firings, transfers, '
                      'suspensions, eligibility rulings, commitments flipping, '
                      'and program developments. Report every distinct '
                      'verified item from the last two days, newest first — '
                      'this feeds a live news wire, so breadth matters'),
            'exclude': 'game recaps and box-score summaries',
        },
    },
    'injuries': {
        'title': 'Injury Report',
        'interval_minutes': 360,
        'job': {
            'key': 'feed_injuries', 'scope': 'league', 'topic': 'injury',
            'section': 'Injury Report', 'window': 3,
            'focus': ('new injury news across FBS college football: fresh '
                      'injuries, surgeries, availability designations, '
                      'game-time decisions, players ruled out or returning. '
                      'Name the player, position, team and status for every '
                      'item. Report every distinct verified item from the '
                      'last three days — this feeds a live injury wire, so '
                      'breadth across many teams matters'),
            'exclude': 'non-medical roster news and old season-ending recaps',
        },
    },
}

_schema_ready = False
_state_lock = threading.Lock()


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_ITEMS_SCHEMA)
            cur.execute(_STATE_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _schema_ready = True
    for feed, spec in FEEDS.items():
        _ensure_state_row(feed, spec)


def _ensure_state_row(feed: str, spec: dict) -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT feed FROM {STATE_TABLE} WHERE feed=%s', (feed,))
            if not cur.fetchone():
                cur.execute(
                    f'INSERT INTO {STATE_TABLE} (feed, enabled, interval_minutes, '
                    f'research_model, search_engine, updated_at) '
                    f'VALUES (%s, 1, %s, %s, %s, %s)',
                    (feed, spec['interval_minutes'], '', '', datetime.utcnow()))
        conn.commit()
    finally:
        conn.close()


_STATE_COLS = ('feed', 'enabled', 'interval_minutes', 'research_model',
               'search_engine', 'last_run_at', 'last_status', 'last_new_items',
               'updated_at')


def get_settings() -> dict:
    """Both feeds' scheduler settings and last-run state, keyed by feed."""
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_STATE_COLS)} FROM {STATE_TABLE}")
            rows = [dict(zip(_STATE_COLS, r)) for r in cur.fetchall() or []]
    finally:
        conn.close()
    out = {}
    for row in rows:
        if row['feed'] not in FEEDS:
            continue
        row['enabled'] = bool(row['enabled'])
        row['title'] = FEEDS[row['feed']]['title']
        for k in ('last_run_at', 'updated_at'):
            if isinstance(row.get(k), datetime):
                row[k] = row[k].isoformat()
        out[row['feed']] = row
    return out


def update_settings(feed: str, patch: dict) -> dict:
    """Validate and store the console's scheduler settings for one feed."""
    ensure_schema()
    if feed not in FEEDS:
        raise FeedError(f"Unknown feed '{feed}'. Feeds: {', '.join(FEEDS)}")
    sets, params = [], []
    if 'enabled' in patch:
        sets.append('enabled=%s')
        params.append(1 if patch['enabled'] else 0)
    if 'interval_minutes' in patch:
        try:
            minutes = int(patch['interval_minutes'])
        except (TypeError, ValueError):
            raise FeedError("'interval_minutes' must be a whole number of minutes.")
        if not MIN_INTERVAL_MIN <= minutes <= MAX_INTERVAL_MIN:
            raise FeedError(f"'interval_minutes' must be between {MIN_INTERVAL_MIN} "
                            f"and {MAX_INTERVAL_MIN}.")
        sets.append('interval_minutes=%s')
        params.append(minutes)
    if 'research_model' in patch:
        model = str(patch['research_model'] or '').strip()
        if model and ('/' not in model or len(model) > 120):
            raise FeedError("'research_model' must be a full OpenRouter model id "
                            "(author/slug), or empty for the service default.")
        sets.append('research_model=%s')
        params.append(model)
    if 'search_engine' in patch:
        engine = str(patch['search_engine'] or '').strip().lower()
        if engine in ('auto', ''):
            engine = ''
        elif engine not in ('native', 'exa'):
            raise FeedError("'search_engine' must be auto, native or exa.")
        sets.append('search_engine=%s')
        params.append(engine)
    if not sets:
        raise FeedError('Nothing to update — send enabled, interval_minutes, '
                        'research_model and/or search_engine.')
    sets.append('updated_at=%s')
    params.append(datetime.utcnow())
    params.append(feed)
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {STATE_TABLE} SET {', '.join(sets)} "
                        f"WHERE feed=%s", tuple(params))
        conn.commit()
    finally:
        conn.close()
    return get_settings()[feed]


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
_ITEM_COLS = ('id', 'feed', 'headline', 'detail', 'team', 'player', 'position',
              'status', 'impact', 'source_name', 'source_url', 'published',
              'confidence', 'created_at')


def items(feed: str, limit: int = 25, before_id: int | None = None) -> list[dict]:
    """One feed's items, newest first. before_id pages into the history."""
    ensure_schema()
    if feed not in FEEDS:
        raise FeedError(f"Unknown feed '{feed}'. Feeds: {', '.join(FEEDS)}")
    limit = max(1, min(int(limit or 25), 100))
    where, params = 'WHERE feed=%s', [feed]
    if before_id:
        where += ' AND id<%s'
        params.append(int(before_id))
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_ITEM_COLS)} FROM {ITEMS_TABLE} {where} "
                f"ORDER BY id DESC LIMIT {limit}", tuple(params))
            rows = [dict(zip(_ITEM_COLS, r)) for r in cur.fetchall() or []]
    finally:
        conn.close()
    for row in rows:
        if isinstance(row.get('created_at'), datetime):
            row['created_at'] = row['created_at'].isoformat()
    return rows


def _item_key(finding: dict) -> str:
    """Dedup identity: the story, not the wording around it."""
    basis = ((finding.get('headline') or '').strip().lower() + '|' +
             (finding.get('source_url') or '').strip().lower())
    return hashlib.sha1(basis.encode('utf-8')).hexdigest()


def _trim(feed: str) -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT id FROM {ITEMS_TABLE} WHERE feed=%s '
                        f'ORDER BY id DESC LIMIT 1 OFFSET %s',
                        (feed, MAX_ITEMS_PER_FEED - 1))
            row = cur.fetchone()
            if row:
                cur.execute(f'DELETE FROM {ITEMS_TABLE} WHERE feed=%s AND id<%s',
                            (feed, int(row[0])))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pulling
# ---------------------------------------------------------------------------
def run_pull(feed: str) -> dict:
    """One research pull for one feed: search, dedup, store, trim."""
    ensure_schema()
    if feed not in FEEDS:
        raise FeedError(f"Unknown feed '{feed}'. Feeds: {', '.join(FEEDS)}")
    state = get_settings().get(feed) or {}
    api_key = db.resolve_openrouter_key()
    if not api_key:
        _set_status(feed, 'No OpenRouter API key configured', 0)
        raise FeedError('No OpenRouter API key is configured.', 500)

    settings = config.default_settings()
    if state.get('research_model'):
        settings['research_model'] = state['research_model']
    if state.get('search_engine'):
        settings['search_engine'] = state['search_engine']

    ctx = {'home_full': 'college football across the FBS', 'away_full': '',
           'home_short': 'FBS', 'away_short': '',
           'year': cfbd_season_year(), 'kickoff': None,
           'now_utc': datetime.utcnow()}
    bucket = research._run_one(api_key, FEEDS[feed]['job'], ctx, settings)

    now = datetime.utcnow()
    found = bucket.get('findings') or []
    inserted = 0
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            for f in found:
                headline = (f.get('headline') or '').strip()[:300]
                if not headline:
                    continue
                key = _item_key(f)
                cur.execute(
                    f'SELECT id FROM {ITEMS_TABLE} WHERE feed=%s AND item_key=%s',
                    (feed, key))
                if cur.fetchone():
                    continue
                cur.execute(
                    f'INSERT INTO {ITEMS_TABLE} (feed, headline, detail, team, '
                    f'player, position, status, impact, source_name, source_url, '
                    f'published, confidence, item_key, created_at) '
                    f'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (feed, headline, (f.get('detail') or '').strip(),
                     (f.get('team') or '').strip()[:80] or None,
                     (f.get('player') or '').strip()[:80] or None,
                     (f.get('position') or '').strip()[:12] or None,
                     (f.get('status') or '').strip()[:60] or None,
                     (f.get('impact') or '').strip()[:300] or None,
                     (f.get('source_name') or '').strip()[:120] or None,
                     (f.get('source_url') or '').strip()[:500] or None,
                     (f.get('published') or '').strip()[:60] or None,
                     (f.get('confidence') or '').strip()[:12] or None,
                     key, now))
                inserted += 1
        conn.commit()
    finally:
        conn.close()
    _trim(feed)

    if bucket.get('error'):
        status = f"Research error: {bucket['error'][:180]}"
    else:
        status = f'OK — {len(found)} found, {inserted} new'
    _set_status(feed, status, inserted)
    logging.info(f"Feed pull '{feed}': {status}")
    return {'feed': feed, 'found': len(found), 'new_items': inserted,
            'status': status}


def cfbd_season_year() -> int:
    import cfbd
    return cfbd.season_year(datetime.utcnow())


def _set_status(feed: str, status: str, new_items: int) -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'UPDATE {STATE_TABLE} SET last_run_at=%s, '
                        f'last_status=%s, last_new_items=%s WHERE feed=%s',
                        (datetime.utcnow(), status[:250], new_items, feed))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------
def _parse_dt(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def claim_due(now: datetime | None = None) -> list[str]:
    """Atomically claim every feed whose interval has elapsed.

    The claim is the UPDATE of last_run_at guarded by its previous value —
    whichever worker's UPDATE lands first wins, so concurrent workers never
    double-pull a feed.
    """
    ensure_schema()
    now = now or datetime.utcnow()
    claimed = []
    for feed, state in get_settings().items():
        if not state['enabled']:
            continue
        last = _parse_dt(state.get('last_run_at'))
        if last is not None and \
                (now - last).total_seconds() < state['interval_minutes'] * 60:
            continue
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                if last is None:
                    cur.execute(f'UPDATE {STATE_TABLE} SET last_run_at=%s '
                                f'WHERE feed=%s AND last_run_at IS NULL',
                                (now, feed))
                else:
                    cur.execute(f'UPDATE {STATE_TABLE} SET last_run_at=%s '
                                f'WHERE feed=%s AND last_run_at=%s',
                                (now, feed, last))
                won = cur.rowcount == 1
            conn.commit()
        finally:
            conn.close()
        if won:
            claimed.append(feed)
    return claimed


def tick(now: datetime | None = None) -> list[str]:
    ran = []
    for feed in claim_due(now):
        try:
            run_pull(feed)
            ran.append(feed)
        except Exception as e:
            logging.exception(f"Feed pull '{feed}' crashed")
            try:
                _set_status(feed, f'Crashed: {e.__class__.__name__}: {e}'[:250], 0)
            except Exception:
                pass
    return ran


def start() -> None:
    """The feed scheduler loop, one daemon thread per worker; the database
    claim in claim_due decides which worker actually pulls."""
    def loop():
        while True:
            time.sleep(60)
            try:
                tick(datetime.utcnow())
            except Exception:
                logging.exception('Feed scheduler tick crashed; continuing')

    threading.Thread(target=loop, daemon=True, name='feeds').start()
