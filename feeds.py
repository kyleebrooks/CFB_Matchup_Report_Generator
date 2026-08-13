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
from datetime import datetime, timedelta

import config
import db
import openrouter
import research

ITEMS_TABLE = 'feed_items'
STATE_TABLE = 'feed_state'
META_TABLE = 'feed_meta'
# Bump when the pull/dedup semantics change enough that old rows would
# mislead: the migration clears both feeds once so the wire restarts clean.
WIRE_LOGIC_VERSION = '3'
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


_META_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {META_TABLE} (
    k VARCHAR(40) PRIMARY KEY,
    v VARCHAR(120) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class FeedError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# The news feed searches from several angles per pull — one broad ask returns a
# couple of headline items and stops, which is how the wire went thin. The
# injury feed does not search at all: it reads the service's injury database
# (the rotowire table, filled by ESPN's structured listing plus every report's
# research and the scheduled sweeps), which is already dated and deduplicated.
FEEDS = {
    'news': {'title': 'Breaking News', 'interval_minutes': 180,
             'window_days': 1},
    'injuries': {'title': 'Injury Report', 'interval_minutes': 360,
                 'window_days': 3},
}

_DATE_RULE = (
    "STRICT FRESHNESS RULE: this wire carries TODAY's news. Only report items "
    "published within the last {days} day(s), and put the article's "
    "publication date in 'published' (ISO format like 2026-08-13). "
    "SOURCE URL RULE: 'source_url' must be the SPECIFIC dated story you read — "
    "a single article with a byline and a publication date. Never cite a "
    "roundup, index, hub, tag, team page or live-blog listing (anything like "
    "/injuries/, /news.php, a homepage, or a 'latest updates' page): the "
    "service verifies each URL's own publication date and DISCARDS anything it "
    "cannot date, so an index link throws the item away. If a roundup is where "
    "you found an item, follow it to the underlying article and cite that.")

NEWS_ANGLES = [
    ('coaching', 'coaching and staff changes across FBS college football: '
                 'hires, firings, resignations, interim appointments, '
                 'coordinator and position-coach moves'),
    ('portal', 'transfer portal entries, transfer commitments, eligibility '
               'and NCAA rulings, and waiver decisions across FBS college '
               'football'),
    ('program', 'suspensions, disciplinary actions, off-field developments '
                'and major program news across FBS college football: '
                'facilities, NIL, scheduling, realignment, quarterback '
                'battles resolved'),
]

# ESPN's structured college-football injury listing is thin — often empty
# outside the season — so the injury wire searches as well, exactly like the
# news wire, with the same page-level date verification.
INJURY_ANGLES = [
    ('breaking', 'college football players newly injured, newly ruled out, '
                 'newly listed as questionable or doubtful, or carted off: '
                 'the injury news that broke today across FBS programs'),
    ('status', 'college football injury STATUS CHANGES and availability '
               'updates: players cleared to return, activated from injury, '
               'upgraded or downgraded for an upcoming game, season-ending '
               'diagnoses confirmed, surgeries scheduled or completed, and '
               'conference availability-report designations just published'),
]


def _injury_jobs(window_days: int, recent_headlines: list[str],
                 last_run_at: str | None = None) -> list[dict]:
    suppress = ''
    if recent_headlines:
        listed = '; '.join(h[:70] for h in recent_headlines[:15])
        suppress = (f". Also exclude injuries already on our wire unless the "
                    f"player's status has actually CHANGED: {listed}")
    anchor = ''
    if last_run_at:
        anchor = (f' Our previous pull ran at {last_run_at} UTC — the target '
                  f'is what changed SINCE then.')
    rule = _DATE_RULE.format(days=window_days)
    return [{
        'key': f'feed_injury_{key}', 'scope': 'league', 'topic': 'injury',
        'section': 'Injury Report', 'window': window_days,
        'focus': f'{focus}. Name the player, position, team and the exact '
                 f'designation for every item — this feeds a live injury '
                 f'wire, so breadth across many programs matters. {rule}'
                 f'{anchor}',
        'exclude': ('transfers, suspensions and other non-medical roster '
                    'news; long-settled injuries with no new development'
                    + suppress),
    } for key, focus in INJURY_ANGLES]


def _news_jobs(window_days: int, recent_headlines: list[str],
               last_run_at: str | None = None) -> list[dict]:
    suppress = ''
    if recent_headlines:
        listed = '; '.join(h[:70] for h in recent_headlines[:15])
        suppress = (f". Also exclude stories already on our wire unless there "
                    f"is a genuinely NEW development: {listed}")
    anchor = ''
    if last_run_at:
        anchor = (f' Our previous pull ran at {last_run_at} UTC — the target '
                  f'is what broke SINCE then.')
    rule = _DATE_RULE.format(days=window_days)
    return [{
        'key': f'feed_news_{key}', 'scope': 'league', 'topic': 'news',
        'section': 'Breaking News', 'window': window_days,
        'focus': f'{focus}. Report every distinct verified item, newest '
                 f'first — this feeds a live news wire, so breadth matters. '
                 f'{rule}{anchor}',
        'exclude': 'game recaps and box-score summaries' + suppress,
    } for key, focus in NEWS_ANGLES]


# ---------------------------------------------------------------------------
# Page-level date verification
# ---------------------------------------------------------------------------
# The model's self-reported dates proved unreliable — it will stamp an old
# article "today" under pressure to deliver. So the wire checks the article
# page itself: standard publication metadata that news CMSes emit. No metadata
# found means no verification, and unverified means not on the wire.
_PUB_PATTERNS = [
    r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)',
    r'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time',
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)',
    r'name=["\'](?:publish-date|publication_date|date)["\'][^>]*content=["\']([^"\']+)',
    r'<time[^>]+datetime=["\']([^"\']+)',
]
_PAGE_FETCH_CAP = 300 * 1024


def _page_published(url: str):
    """The article page's own publication date, or None. Never raises."""
    import re
    import requests
    import injuries as injuries_mod
    try:
        resp = requests.get(
            url, timeout=6, stream=True,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; CFBReportsWire/1.0)'})
        if resp.status_code >= 400:
            return None
        raw = b''
        for chunk in resp.iter_content(chunk_size=32 * 1024):
            raw += chunk
            if len(raw) >= _PAGE_FETCH_CAP:
                break
        resp.close()
        html = raw.decode('utf-8', 'replace')
    except Exception:
        return None
    for pattern in _PUB_PATTERNS:
        m = re.search(pattern, html, re.IGNORECASE)
        if not m:
            continue
        value = m.group(1).strip()
        try:
            return datetime.fromisoformat(value[:10]).date()
        except ValueError:
            parsed = injuries_mod.parse_date_text(value)
            if parsed:
                return parsed
    return None

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
            cur.execute(_META_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _schema_ready = True
    for feed, spec in FEEDS.items():
        _ensure_state_row(feed, spec)
    _migrate()


def _get_meta(key: str) -> str | None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT v FROM {META_TABLE} WHERE k=%s', (key,))
            row = cur.fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _set_meta(key: str, value: str) -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM {META_TABLE} WHERE k=%s', (key,))
            cur.execute(f'INSERT INTO {META_TABLE} (k, v) VALUES (%s, %s)',
                        (key, value))
        conn.commit()
    finally:
        conn.close()


def _migrate() -> None:
    if _get_meta('wire_logic') == WIRE_LOGIC_VERSION:
        return
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM {ITEMS_TABLE}')
        conn.commit()
    finally:
        conn.close()
    _set_meta('wire_logic', WIRE_LOGIC_VERSION)
    logging.info('Wire logic version changed — both feeds cleared for a clean start')


def clear(feed: str) -> dict:
    """Empty one feed's items on demand (the console's Clear button)."""
    ensure_schema()
    if feed not in FEEDS:
        raise FeedError(f"Unknown feed '{feed}'. Feeds: {', '.join(FEEDS)}")
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DELETE FROM {ITEMS_TABLE} WHERE feed=%s', (feed,))
            removed = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    _set_status(feed, f'Cleared — {removed} item(s) removed', 0)
    return {'feed': feed, 'removed': removed}


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


def _norm(text: str) -> str:
    import re
    return ' '.join(re.sub(r'[^a-z0-9 ]+', ' ', (text or '').lower()).split())


def _item_key(feed: str, finding: dict) -> str:
    """Dedup identity: the STORY, not its wording or its URL.

    The first version hashed headline+URL, so the same story re-reported with
    different phrasing or from a different outlet re-entered the wire every
    pull. News now keys on the normalized headline alone; injuries key on
    team+player+headline, so a genuine status change (a new headline) is news
    while a re-report of the same designation is not.
    """
    if feed == 'injuries':
        # The designation is part of the identity: Questionable -> Out is the
        # update the wire exists to carry, while the same status re-reported
        # is not.
        basis = '|'.join(_norm(finding.get(k))
                         for k in ('team', 'player', 'headline', 'status'))
    else:
        basis = _norm(finding.get('headline'))
    return hashlib.sha1(basis.encode('utf-8')).hexdigest()


def _url_seen(cur, feed: str, url: str) -> bool:
    """A second dedup gate: the exact article can only enter the wire once,
    however differently the model words its headline this pull."""
    if not url:
        return False
    cur.execute(f'SELECT id FROM {ITEMS_TABLE} WHERE feed=%s AND source_url=%s '
                f'LIMIT 1', (feed, url.strip()))
    return cur.fetchone() is not None


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
def _store_items(feed: str, findings: list[dict], window_days: int,
                 verify_pages: bool = False) -> dict:
    """The verification gate and the write: date-check, dedup, insert.

    With verify_pages the claimed date is not trusted at all: the article page
    itself must carry a publication date (standard CMS metadata), and that
    date must fall inside the window. No URL, no metadata, or an old page —
    not on the wire. Store-sourced feeds skip the fetch; their dates are
    already structural.
    """
    import injuries as injuries_mod
    now = datetime.utcnow()
    today = now.date()
    inserted = stale = undated = 0
    checked_urls: dict[str, object] = {}
    # Why items were turned away, with examples — an empty wire should be
    # explainable from the logs alone, not by re-deriving it each time.
    rejects: list[str] = []
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            for f in findings:
                headline = (f.get('headline') or '').strip()[:300]
                if not headline:
                    continue
                if verify_pages:
                    url = (f.get('source_url') or '').strip()
                    if not url:
                        undated += 1
                        rejects.append(f'no source url: {headline[:60]}')
                        continue
                    if url not in checked_urls:
                        checked_urls[url] = _page_published(url)
                    day = checked_urls[url]
                    if day is None:
                        undated += 1
                        rejects.append(f'page carries no publication date '
                                       f'(index/hub page?): {url[:90]}')
                        continue
                else:
                    day = injuries_mod.parse_date_text(f.get('published') or '')
                    if day is None:
                        undated += 1
                        rejects.append(f'undatable: {headline[:60]}')
                        continue
                if (today - day).days > window_days or day > today:
                    stale += 1
                    rejects.append(f'published {day}, outside the '
                                   f'{window_days}-day window: {headline[:60]}')
                    continue
                key = _item_key(feed, f)
                cur.execute(
                    f'SELECT id FROM {ITEMS_TABLE} WHERE feed=%s AND item_key=%s',
                    (feed, key))
                if cur.fetchone():
                    continue
                # The one-article-once gate is a NEWS rule: an injury page is
                # the standing source for a player and legitimately reports
                # his next status change from the same URL.
                if feed != 'injuries' and _url_seen(cur, feed,
                                                    f.get('source_url') or ''):
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
                     day.isoformat(),
                     (f.get('confidence') or '').strip()[:12] or None,
                     key, now))
                inserted += 1
        conn.commit()
    finally:
        conn.close()
    _trim(feed)
    if rejects:
        logging.info(f"Feed '{feed}' turned away {len(rejects)} item(s); "
                     f"first few: " + ' | '.join(rejects[:5]))
    return {'inserted': inserted, 'stale': stale, 'undated': undated}


def _pull_news(state: dict) -> dict:
    """Three concurrent search angles, suppression of what is already here."""
    api_key = db.resolve_openrouter_key()
    if not api_key:
        _set_status('news', 'No OpenRouter API key configured', 0)
        raise FeedError('No OpenRouter API key is configured.', 500)
    settings = config.default_settings()
    if state.get('research_model'):
        settings['research_model'] = state['research_model']
    if state.get('search_engine'):
        settings['search_engine'] = state['search_engine']

    window_days = FEEDS['news']['window_days']
    recent = [r['headline'] for r in items('news', limit=25)]
    jobs = _news_jobs(window_days, recent, last_run_at=state.get('last_run_at'))
    ctx = {'home_full': 'college football across the FBS', 'away_full': '',
           'home_short': 'FBS', 'away_short': '',
           'year': cfbd_season_year(), 'kickoff': None,
           'now_utc': datetime.utcnow()}
    raw = research.run_research(api_key, ctx, settings, jobs=jobs)
    findings, errors = [], []
    for job in jobs:
        bucket = raw.get(job['key']) or {}
        findings.extend(bucket.get('findings') or [])
        if bucket.get('error'):
            errors.append(bucket['error'][:80])
    counts = _store_items('news', findings, window_days, verify_pages=True)
    if errors and not findings:
        status = f"Research error: {'; '.join(errors)[:180]}"
    else:
        status = (f"OK — {len(findings)} found across {len(jobs)} searches, "
                  f"{counts['inserted']} new, {counts['stale']} page-dated old "
                  f"and dropped, {counts['undated']} unverifiable and dropped")
    return {'found': len(findings), 'counts': counts, 'status': status}


_FIRST_LISTED_RE = None


def _first_listed(detail: str):
    """When ESPN first listed this designation, or None if it does not say.

    ESPN stamps every fetch 'today', which would keep months-old standing
    designations permanently fresh. The real signal is the 'First listed
    YYYY-MM-DD' marker the collector embeds — a designation is NEW when ESPN
    first listed it recently, not when we last looked. No marker means we
    cannot tell, and on a breaking wire that means it does not run.
    """
    global _FIRST_LISTED_RE
    if _FIRST_LISTED_RE is None:
        import re
        _FIRST_LISTED_RE = re.compile(r'First listed (\d{4}-\d{2}-\d{2})')
    m = _FIRST_LISTED_RE.search(detail or '')
    return m.group(1) if m else None


def _refresh_injury_store() -> tuple[int, int]:
    """Pull ESPN's current designations into the injury database, free.

    Slate teams (upcoming ranked games) when the season provides them, the
    full FBS list otherwise — the feed must not starve in weeks when no
    report run or sweep happens to have filled the store.
    """
    import injuries as injuries_mod
    from concurrent.futures import ThreadPoolExecutor
    try:
        teams = injuries_mod.slate_teams()
    except Exception:
        teams = []
    if not teams:
        try:
            teams = injuries_mod.fbs_teams()
        except Exception as e:
            logging.warning(f'FBS team list unavailable for ESPN refresh: {e}')
            return 0, 0
    teams = teams[:60]
    inserted = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for out in pool.map(
                lambda t: injuries_mod._collect_espn(t['short'], t['full']),
                teams):
            inserted += out.get('inserted', 0)
    return len(teams), inserted


def _known_statuses() -> dict:
    """The status the wire last carried per (team, player) — the baseline a
    change is measured against."""
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT team, player, status FROM {ITEMS_TABLE} '
                f"WHERE feed='injuries' AND player IS NOT NULL ORDER BY id ASC")
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return {(_norm(r[0]), _norm(r[1])): (r[2] or '') for r in rows}


def _pull_injuries(state: dict) -> dict:
    """Two sources, one wire: ESPN's structured designations and live research.

    ESPN's college-football injury listing is authoritative when it has data
    but is frequently empty (it carried nothing for any major program in
    mid-August 2026), so it cannot be the only source. The pull refreshes the
    injury database from ESPN, materializes designations that are genuinely
    new — first listed recently, or a STATUS CHANGE against what the wire
    already carries — and then searches for the injury news that broke since
    the last pull, verified against each article page's own date exactly like
    the news wire.
    """
    import injuries as injuries_mod
    window_days = FEEDS['injuries']['window_days']
    teams_checked, _ = _refresh_injury_store()
    known = _known_statuses()
    today_iso = datetime.utcnow().date().isoformat()

    # --- source 1: the injury database (ESPN + report research + sweeps) ----
    # The window is the wire's own — the database keeps a fortnight for the
    # reports, but a fortnight-old designation is not breaking news.
    cutoff = (datetime.utcnow().date() - timedelta(days=window_days)).isoformat()
    conn = injuries_mod._connect()
    try:
        rows = conn.execute(
            f"SELECT team_name, player_name, headline, news_text, position, "
            f"status, analysis_text, source_name, source_url, news_date, "
            f"confidence, provider FROM {injuries_mod.TABLE} "
            f"WHERE news_date >= ? AND confidence != 'low' "
            f"ORDER BY news_date DESC, id DESC LIMIT 400", (cutoff,)).fetchall()
    finally:
        conn.close()

    # Provenance decides how a row earns its date. ESPN's designations are
    # structured, so their own first-listed marker is trustworthy. Everything
    # else in this table came from a research call whose date the model
    # asserted — the same unreliable claim that put January stories on the
    # news wire — so those rows must clear the article page like any other.
    espn_findings, article_findings = [], []
    for r in rows:
        team, player, status = r[0], r[1], (r[5] or '')
        seen = known.get((_norm(team), _norm(player)))
        changed = seen is not None and _norm(seen) != _norm(status)
        item = {
            'headline': r[2], 'detail': r[3], 'team': team, 'player': player,
            'position': r[4], 'status': status, 'impact': r[6],
            'source_name': r[7], 'source_url': r[8], 'confidence': r[10],
        }
        from_espn = ((r[11] or '').strip().lower() == injuries_mod.PROVIDER_ESPN
                     or (r[7] or '').strip().upper() == 'ESPN')
        if from_espn:
            first = _first_listed(r[3])
            if changed:
                item['published'] = today_iso     # the change is today's news
            elif first:
                item['published'] = first
            else:
                continue          # undatable standing designation: not new
            espn_findings.append(item)
        else:
            item['published'] = r[9]
            article_findings.append(item)

    store_counts = _store_items('injuries', espn_findings, window_days)
    article_counts = _store_items('injuries', article_findings, window_days,
                                  verify_pages=True)
    for k in store_counts:
        store_counts[k] += article_counts[k]

    # --- source 2: live research, page-verified like the news wire ----------
    research_counts = {'inserted': 0, 'stale': 0, 'undated': 0}
    found_research = 0
    api_key = db.resolve_openrouter_key()
    if api_key:
        settings = config.default_settings()
        if state.get('research_model'):
            settings['research_model'] = state['research_model']
        if state.get('search_engine'):
            settings['search_engine'] = state['search_engine']
        recent = [r['headline'] for r in items('injuries', limit=25)]
        jobs = _injury_jobs(window_days, recent,
                            last_run_at=state.get('last_run_at'))
        ctx = {'home_full': 'college football across the FBS', 'away_full': '',
               'home_short': 'FBS', 'away_short': '',
               'year': cfbd_season_year(), 'kickoff': None,
               'now_utc': datetime.utcnow()}
        try:
            raw = research.run_research(api_key, ctx, settings, jobs=jobs)
        except Exception as e:
            logging.warning(f'Injury research failed (ESPN data still used): {e}')
            raw = {}
        research_findings = []
        for job in jobs:
            research_findings.extend((raw.get(job['key']) or {}).get('findings')
                                     or [])
        found_research = len(research_findings)
        research_counts = _store_items('injuries', research_findings,
                                       window_days, verify_pages=True)

    counts = {k: store_counts[k] + research_counts[k]
              for k in ('inserted', 'stale', 'undated')}
    from_db = len(espn_findings) + len(article_findings)
    status = (f"OK — ESPN refreshed across {teams_checked} team(s); "
              f"{len(espn_findings)} ESPN + {len(article_findings)} stored "
              f"article row(s) + {found_research} researched item(s); "
              f"{counts['inserted']} new, {counts['stale']} not current, "
              f"{counts['undated']} unverifiable")
    return {'found': from_db + found_research, 'counts': counts,
            'status': status}


def run_pull(feed: str) -> dict:
    """One pull for one feed: gather, verify, dedup, store, trim."""
    ensure_schema()
    if feed not in FEEDS:
        raise FeedError(f"Unknown feed '{feed}'. Feeds: {', '.join(FEEDS)}")
    state = get_settings().get(feed) or {}
    out = _pull_news(state) if feed == 'news' else _pull_injuries(state)
    inserted = out['counts']['inserted']
    _set_status(feed, out['status'], inserted)
    logging.info(f"Feed pull '{feed}': {out['status']}")
    return {'feed': feed, 'found': out['found'], 'new_items': inserted,
            'status': out['status']}


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
