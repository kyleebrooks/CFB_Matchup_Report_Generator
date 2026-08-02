"""The injury feed: collection, caching and storage.

Bright Data is gone. The feed is now filled by the same OpenRouter + Exa research layer
the reports already use, which means no scraper to be blocked, no second vendor, and no
scheduled job that can fail silently for a season.

Two paths write to it, and the cheap one carries almost all the traffic:

  record_findings()  Every matchup and team report already makes a per-team injury
                     research call. Persisting what it found costs nothing extra, so
                     the feed fills in as a side effect of normal use.

  ensure_fresh()     For a team nobody has reported on recently, make a call — but only
                     if the cached rows are older than INJURY_FEED_TTL_HOURS. This is
                     what keeps "as of that moment" honest without paying for a sweep of
                     all 136 FBS teams twice a day.

Rows land in the same `rotowire` table the reports already read, so nothing downstream
had to change. The table keeps its name because that is what is deployed; the data in
it is no longer Rotowire's.
"""

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import config
import db

TABLE = 'rotowire'
REFRESH_TABLE = 'feed_refresh'

# Providers are registered here so a structured source (ESPN, a licensed Rotowire feed,
# conference availability reports) can be added later without touching any caller.
PROVIDER_RESEARCH = 'research'
PROVIDER_ESPN = 'espn'

# feed_refresh keys that are not teams (the sweep claim row). Kept out of coverage math.
SYSTEM_KEY_PREFIX = '_'

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _team_lock(key: str) -> threading.Lock:
    """One refresh at a time per team — a matchup report asks for two teams at once."""
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def team_key(short_name: str, full_name: str = '') -> str:
    text = (short_name or full_name or '').strip().lower()
    return re.sub(r'[^a-z0-9]+', '-', text).strip('-')


# ---------------------------------------------------------------------------
# Schema — additive, so an existing feed database keeps every row it has
# ---------------------------------------------------------------------------
NEW_COLUMNS = (
    ('source_url', 'TEXT'),    # the article itself, so citations point somewhere real
    ('status', 'TEXT'),        # out / questionable / probable / available
    ('news_date', 'TEXT'),     # ISO date, immune to date_text formatting drift
    ('fetched_at', 'TEXT'),
    ('provider', 'TEXT'),
    ('confidence', 'TEXT'),    # high/medium/low; low when the item could not be dated
)


def ensure_schema(conn) -> None:
    have = {row[1] for row in conn.execute(f'PRAGMA table_info({TABLE})')}
    for name, decl in NEW_COLUMNS:
        if name not in have:
            conn.execute(f'ALTER TABLE {TABLE} ADD COLUMN {name} {decl}')
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REFRESH_TABLE} (
            team_key    TEXT PRIMARY KEY,
            team_name   TEXT,
            provider    TEXT,
            fetched_at  TEXT,
            ok          INTEGER,
            items       INTEGER,
            inserted    INTEGER,
            error       TEXT
        )
        """
    )
    conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_news_date '
                 f'ON {TABLE} (news_date)')


def _connect():
    conn = db.get_rotowire_db_connection()
    with conn:
        ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
# db.format_friendly_date() produces exactly this shape, and the legacy read path
# matched date_text against it by string equality. New rows also carry an ISO
# news_date, so a change of shape can no longer empty the feed — but the check stays,
# because rows written before that column existed still depend on it.
_PARSE_FORMATS = (
    '%B %d, %Y', '%b %d, %Y', '%B %d %Y', '%b %d %Y',
    '%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%d %B %Y', '%b. %d, %Y',
)
_RELATIVE = re.compile(r'^(\d+)\s*(minute|min|hour|hr|day|week)s?\s+ago$')


def parse_date_text(text: str, today=None):
    """Best-effort parse of whatever is in date_text. None when unrecognised."""
    raw = (text or '').strip()
    if not raw:
        return None
    today = today or datetime.now().date()
    low = raw.lower().rstrip('.')

    if low in ('today', 'now', 'just now'):
        return today
    if low == 'yesterday':
        return today - timedelta(days=1)
    m = _RELATIVE.match(low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit in ('minute', 'min', 'hour', 'hr'):
            return today
        return today - timedelta(days=n * (7 if unit == 'week' else 1))

    for fmt in _PARSE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    # A month and day with no year ("August 1") — assume the most recent occurrence.
    for fmt in ('%B %d', '%b %d'):
        try:
            guess = datetime.strptime(raw, fmt).date().replace(year=today.year)
            return guess if guess <= today else guess.replace(year=today.year - 1)
        except ValueError:
            continue
    return None


def matches_report_query(text: str) -> bool:
    """True when a legacy row's date_text is one the exact-string filter could match."""
    parsed = parse_date_text(text)
    if parsed is None:
        return False
    return db.format_friendly_date(
        datetime.combine(parsed, datetime.min.time())) == (text or '').strip()


def _news_date(published: str):
    """Resolve a finding's publish date. Returns (date, dated).

    Undateable items are stamped today so they enter the read window — the model is
    told to discard anything it cannot date, so an unparseable value usually means
    "current but loosely worded" ("Monday", "this morning"). `dated=False` marks the
    guess, and store() downgrades such rows to low confidence rather than letting an
    assumed date masquerade as verified freshness.
    """
    parsed = parse_date_text(published)
    if parsed and parsed <= datetime.now().date():
        return parsed, True
    return datetime.now().date(), False


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def store(team_short: str, team_full: str, findings: list[dict],
          provider: str = PROVIDER_RESEARCH) -> dict:
    """Upsert findings for one team. Returns {items, inserted, refreshed}.

    Matched on (team, player, headline) rather than on every column: the same item
    re-reported with slightly different wording should refresh the existing row, not
    accumulate a near-duplicate every six hours.
    """
    result = {'items': 0, 'inserted': 0, 'refreshed': 0}
    rows = [f for f in (findings or []) if isinstance(f, dict)]
    if not rows:
        return result

    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    conn = _connect()
    try:
        with conn:
            for f in rows:
                headline = (f.get('headline') or '').strip()
                detail = (f.get('detail') or '').strip()
                if not headline and not detail:
                    continue
                result['items'] += 1

                player = (f.get('player') or '').strip()
                team = (f.get('team') or '').strip() or team_full or team_short
                day, dated = _news_date(f.get('published') or '')
                confidence = (f.get('confidence') or '').strip().lower() or 'medium'
                if not dated:
                    # An assumed date must not read as verified freshness.
                    confidence = 'low'
                values = {
                    'player_name': player,
                    'headline': headline,
                    'team_name': team,
                    'date_text': db.format_friendly_date(
                        datetime.combine(day, datetime.min.time())),
                    'news_text': detail,
                    'source_name': (f.get('source_name') or '').strip() or 'Web research',
                    'position': (f.get('position') or '').strip(),
                    'analysis_text': (f.get('impact') or '').strip(),
                    'source_url': (f.get('source_url') or '').strip(),
                    'status': (f.get('status') or '').strip(),
                    'news_date': day.isoformat(),
                    'fetched_at': now,
                    'provider': provider,
                    'confidence': confidence,
                }

                existing = conn.execute(
                    f'SELECT id FROM {TABLE} WHERE team_name=? AND player_name=? '
                    f'AND headline=? LIMIT 1', (team, player, headline)).fetchone()
                if existing:
                    conn.execute(
                        f"UPDATE {TABLE} SET news_text=?, analysis_text=?, status=?, "
                        f"source_url=?, source_name=?, position=?, date_text=?, "
                        f"news_date=?, fetched_at=?, provider=?, confidence=? WHERE id=?",
                        (values['news_text'], values['analysis_text'], values['status'],
                         values['source_url'], values['source_name'], values['position'],
                         values['date_text'], values['news_date'], now, provider,
                         values['confidence'], existing[0]))
                    result['refreshed'] += 1
                else:
                    cols = ','.join(values)
                    conn.execute(
                        f"INSERT INTO {TABLE} ({cols}) "
                        f"VALUES ({','.join('?' * len(values))})", tuple(values.values()))
                    result['inserted'] += 1
    finally:
        conn.close()
    return result


def record_refresh(key: str, team_name: str, ok: bool, items: int = 0,
                   inserted: int = 0, error: str = '',
                   provider: str = PROVIDER_RESEARCH) -> None:
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    f"INSERT INTO {REFRESH_TABLE} (team_key, team_name, provider, "
                    f"fetched_at, ok, items, inserted, error) VALUES (?,?,?,?,?,?,?,?) "
                    f"ON CONFLICT(team_key) DO UPDATE SET team_name=excluded.team_name, "
                    f"provider=excluded.provider, fetched_at=excluded.fetched_at, "
                    f"ok=excluded.ok, items=excluded.items, inserted=excluded.inserted, "
                    f"error=excluded.error",
                    (key, team_name, provider,
                     datetime.now(timezone.utc).isoformat(timespec='seconds'),
                     1 if ok else 0, items, inserted, (error or '')[:400]))
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"Could not record the injury refresh (non-fatal): {e}")


def last_refresh(key: str) -> dict | None:
    try:
        conn = _connect()
        try:
            row = conn.execute(
                f'SELECT team_key, team_name, provider, fetched_at, ok, items, inserted, '
                f'error FROM {REFRESH_TABLE} WHERE team_key=?', (key,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"Refresh history unavailable: {e}")
        return None


def age_hours(entry: dict | None) -> float | None:
    if not entry or not entry.get('fetched_at'):
        return None
    try:
        stamp = datetime.fromisoformat(entry['fetched_at'])
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def _job(team_full: str) -> dict:
    """A single-team injury research job, shaped like research.RESEARCH_JOBS entries."""
    return {
        'key': 'feed_injuries',
        'scope': 'home',
        'topic': 'injury',
        'section': f'{team_full} Injury Report',
        'window': config.INJURY_WINDOW_DAYS,
        'focus': (
            'injuries, injury designations, availability rulings, surgeries, return '
            'timelines, medical redshirts, and week-to-week statuses for CURRENT players '
            'on {team_full}. Include the conference availability report for {team_full} '
            'if one has been published (the SEC, Big Ten, Big 12 and the College Football '
            'Playoff all require them), and give each player their listed designation'
        ),
        'exclude': 'suspensions, transfers, depth-chart moves, and any non-medical roster news',
    }


def _collect_espn(team_short: str, team_full: str) -> dict:
    """Pull ESPN's structured designations into the feed. Free, and never raises."""
    if not config.INJURY_ESPN_ENABLED:
        return {'items': 0, 'inserted': 0, 'refreshed': 0}
    try:
        import espn_injuries
        findings = espn_injuries.fetch_team_injuries(team_short, team_full)
        if not findings:
            return {'items': 0, 'inserted': 0, 'refreshed': 0}
        return store(team_short, team_full, findings, provider=PROVIDER_ESPN)
    except Exception as e:
        logging.warning(f"ESPN injury lookup for {team_full} failed (non-fatal): {e}")
        return {'items': 0, 'inserted': 0, 'refreshed': 0}


def refresh_team(team_short: str, team_full: str = '', settings: dict | None = None,
                 api_key: str | None = None) -> dict:
    """Fetch this team's current injuries and store them. Never raises.

    Two sources per refresh: ESPN's structured designation list (free, deterministic)
    and a web-research pass (broad, catches what a structured feed hasn't listed yet).
    ESPN failing never blocks research, and vice versa.
    """
    import research

    team_full = team_full or team_short
    key = team_key(team_short, team_full)
    out = {'team': team_full, 'team_key': key, 'ok': False, 'items': 0,
           'inserted': 0, 'refreshed': 0, 'error': None,
           'provider': PROVIDER_RESEARCH}

    with _team_lock(key):
        espn = _collect_espn(team_short, team_full)
        out['espn_items'] = espn['items']

        try:
            api_key = api_key or db.resolve_openrouter_key()
        except Exception as e:
            out['error'] = f'Could not read the OpenRouter key: {e}'
            record_refresh(key, team_full, False, items=espn['items'],
                           inserted=espn['inserted'], error=out['error'])
            return out
        if not api_key:
            out['error'] = ("No OpenRouter key. Add an 'openrouter' row to API_KEYS "
                            "or set OPENROUTER_API_KEY.")
            record_refresh(key, team_full, False, items=espn['items'],
                           inserted=espn['inserted'], error=out['error'])
            return out

        # Injury collection digs deeper than the general research default: the
        # designation that matters is often in beat reporting, not the top results.
        settings = dict(settings or config.default_settings())
        settings['search_max_results'] = max(
            int(settings.get('search_max_results') or 0),
            config.INJURY_SEARCH_MAX_RESULTS)
        import cfbd
        ctx = research.build_context(team_full, team_full, team_short, team_short,
                                     cfbd.season_year(datetime.now()), None)
        # A single-team job: the shared prompt builder addresses both sides of a matchup,
        # so pointing both at this team keeps the recency and no-fabrication rules while
        # scoping the search to one program.
        bucket = research._run_one(api_key, _job(team_full), ctx, settings)

        if bucket.get('error'):
            out['error'] = bucket['error']
            # ESPN may still have delivered; record the failure so retries come soon,
            # but keep its counts visible.
            record_refresh(key, team_full, False, items=espn['items'],
                           inserted=espn['inserted'], error=out['error'])
            return out

        stored = store(team_short, team_full, bucket.get('findings') or [])
        items = stored['items'] + espn['items']
        inserted = stored['inserted'] + espn['inserted']
        out.update(ok=True, items=items, inserted=inserted,
                   refreshed=stored['refreshed'] + espn['refreshed'],
                   notes=(bucket.get('notes') or '')[:300])
        record_refresh(key, team_full, True, items, inserted)
        logging.info(f"Injury feed refreshed for {team_full}: {items} items "
                     f"({espn['items']} from ESPN), {inserted} new")
        return out


def record_findings(team_short: str, team_full: str, findings: list[dict],
                    succeeded: bool = True, error: str = '') -> dict:
    """Persist injury findings a report already paid for. This is the free path.

    A successful research pass that found NOTHING is still a refresh — the team is
    healthy and the fact is fresh — so it marks the cache and stops ensure_fresh()
    from paying for an identical call minutes later. A FAILED pass must never mark
    the team fresh: it records the failure so the short retry window applies.
    """
    key = team_key(team_short, team_full)
    try:
        stored = store(team_short, team_full, findings)
        if succeeded:
            record_refresh(key, team_full or team_short, True,
                           stored['items'], stored['inserted'])
        else:
            record_refresh(key, team_full or team_short, False,
                           stored['items'], stored['inserted'],
                           error=(error or 'research call failed')[:400])
        return stored
    except Exception as e:
        logging.warning(f"Could not persist injury findings for {team_full}: {e}")
        return {'items': 0, 'inserted': 0, 'refreshed': 0}


def ensure_fresh(team_short: str, team_full: str = '', settings: dict | None = None,
                 api_key: str | None = None, ttl_hours: float | None = None) -> list[dict]:
    """Return this team's feed rows, refreshing first only if the cache has expired.

    A refresh failure is never fatal: stale rows beat no rows, and the report's own live
    research call covers the same ground independently. A cache entry whose last attempt
    FAILED only suppresses retries for the short retry window, not the full TTL —
    stale-with-an-error is the one state worth paying to escape quickly.
    """
    team_full = team_full or team_short
    ttl = config.INJURY_FEED_TTL_HOURS if ttl_hours is None else ttl_hours
    key = team_key(team_short, team_full)

    entry = last_refresh(key)
    age = age_hours(entry)
    effective_ttl = ttl if (entry or {}).get('ok') else min(ttl, config.INJURY_RETRY_HOURS)
    if age is None or age >= effective_ttl:
        try:
            refresh_team(team_short, team_full, settings, api_key)
        except Exception as e:
            logging.warning(f"Injury refresh for {team_full} failed: {e}")

    try:
        return db.fetch_rotowire_for_team(team_short, team_full,
                                          days=config.INJURY_WINDOW_DAYS)
    except Exception as e:
        logging.warning(f"Injury feed lookup for {team_full} failed: {e}")
        return []


def ensure_fresh_pair(home_short, home_full, away_short, away_full,
                      settings=None, api_key=None) -> dict:
    """Both sides of a matchup, refreshed concurrently."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        home = pool.submit(ensure_fresh, home_short, home_full, settings, api_key)
        away = pool.submit(ensure_fresh, away_short, away_full, settings, api_key)
        return {'home': home.result(), 'away': away.result()}


def sweep(teams: list[dict], settings: dict | None = None, force: bool = False,
          ttl_hours: float | None = None, progress=None) -> list[dict]:
    """Refresh many teams. `teams` is [{'short': ..., 'full': ...}].

    Used by the CLI, not by the request path — a full FBS sweep is a deliberate act with
    a real cost, so nothing triggers it automatically.
    """
    ttl = config.INJURY_FEED_TTL_HOURS if ttl_hours is None else ttl_hours
    api_key = db.resolve_openrouter_key()
    settings = settings or config.default_settings()
    results: list[dict] = []

    def one(team):
        short = team.get('short') or team.get('full')
        full = team.get('full') or short
        key = team_key(short, full)
        if not force:
            age = age_hours(last_refresh(key))
            if age is not None and age < ttl:
                return {'team': full, 'team_key': key, 'ok': True, 'skipped': True,
                        'items': 0, 'inserted': 0,
                        'error': None, 'age_hours': round(age, 1)}
        return refresh_team(short, full, settings, api_key)

    with ThreadPoolExecutor(max_workers=config.RESEARCH_MAX_WORKERS) as pool:
        for result in pool.map(one, teams):
            results.append(result)
            if progress:
                progress(result, len(results), len(teams))
    return results


# ---------------------------------------------------------------------------
# Per-player availability — the latest word wins
# ---------------------------------------------------------------------------
def resolve_availability(items: list[dict]) -> list[dict]:
    """Collapse feed rows to one current status per player.

    The raw feed keeps the whole timeline ("Questionable" Tuesday, "Out" Friday), which
    is the right thing to store but the wrong thing to make a report model reconcile.
    This picks each player's most recent dated word — preferring rows that actually
    carry a designation — so the report gets a clean availability table alongside the
    timeline.
    """
    by_player: dict = {}
    for it in items or []:
        player = (it.get('player') or '').strip()
        if not player:
            continue
        key = player.lower()
        day = (it.get('news_date') or '').strip()
        if not day:
            parsed = parse_date_text(it.get('date') or '')
            day = parsed.isoformat() if parsed else ''
        candidate = {
            'player': player,
            'position': (it.get('position') or '').strip(),
            'status': (it.get('status') or '').strip(),
            'as_of': day,
            'headline': (it.get('headline') or '').strip(),
            'confidence': (it.get('confidence') or '').strip() or 'medium',
            'provider': (it.get('provider') or '').strip() or PROVIDER_RESEARCH,
            'source_name': (it.get('source_name') or '').strip(),
        }
        held = by_player.get(key)
        if held is None:
            by_player[key] = candidate
            continue
        # Newer wins; on the same day, a row WITH a designation beats one without.
        if (candidate['as_of'], bool(candidate['status'])) > \
           (held['as_of'], bool(held['status'])):
            by_player[key] = candidate
    out = sorted(by_player.values(), key=lambda p: (p['as_of'], p['player']), reverse=True)
    return out


# ---------------------------------------------------------------------------
# The scheduled game-aware sweep
# ---------------------------------------------------------------------------
SWEEP_KEY = '_sweep'


def sweep_slots() -> list[tuple[int, int]]:
    """Parse INJURY_SWEEP_SLOTS ('dow:hour_utc,...', 0=Mon) into (dow, hour) pairs."""
    slots = []
    for part in (config.INJURY_SWEEP_SLOTS or '').split(','):
        part = part.strip()
        if not part or ':' not in part:
            continue
        try:
            dow, hour = (int(x) for x in part.split(':', 1))
        except ValueError:
            continue
        if 0 <= dow <= 6 and 0 <= hour <= 23:
            slots.append((dow, hour))
    return slots


def _claim_sweep(now: datetime, guard_hours: float = 20.0) -> bool:
    """Atomically claim this sweep slot in the shared feed database.

    Same idea as the report scheduler's claim: whichever worker lands the guarded
    UPDATE first wins, so several gunicorn workers never sweep concurrently.
    """
    stamp = now.replace(tzinfo=timezone.utc).isoformat(timespec='seconds') \
        if now.tzinfo is None else now.isoformat(timespec='seconds')
    try:
        conn = _connect()
        try:
            with conn:
                row = conn.execute(
                    f'SELECT fetched_at FROM {REFRESH_TABLE} WHERE team_key=?',
                    (SWEEP_KEY,)).fetchone()
                if row is None:
                    cur = conn.execute(
                        f'INSERT OR IGNORE INTO {REFRESH_TABLE} '
                        f'(team_key, team_name, provider, fetched_at, ok, items, '
                        f'inserted, error) VALUES (?,?,?,?,1,0,0,?)',
                        (SWEEP_KEY, 'Scheduled injury sweep', PROVIDER_RESEARCH,
                         stamp, ''))
                    return cur.rowcount == 1
                age = age_hours({'fetched_at': row['fetched_at']})
                if age is not None and age < guard_hours:
                    return False
                cur = conn.execute(
                    f'UPDATE {REFRESH_TABLE} SET fetched_at=? '
                    f'WHERE team_key=? AND fetched_at=?',
                    (stamp, SWEEP_KEY, row['fetched_at']))
                return cur.rowcount == 1
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"Sweep claim failed; skipping this slot: {e}")
        return False


def slate_teams(lookahead_days: int | None = None) -> list[dict]:
    """Teams in upcoming Top-25 games: both sides of any game inside the lookahead
    window where either side is AP-ranked. The ranked team's opponent matters exactly
    as much to that matchup, so it is swept too."""
    import weekly

    api_key = db.resolve_cfbd_key()
    if not api_key:
        return []
    lookahead = (config.INJURY_SWEEP_LOOKAHEAD_DAYS
                 if lookahead_days is None else lookahead_days)
    data = weekly.build_week_data(api_key, completed=False)
    horizon = datetime.now(timezone.utc) + timedelta(days=lookahead)

    seen, teams = set(), []
    for g in data['games']:
        if not (g.get('home_rank') or g.get('away_rank')):
            continue
        start = None
        raw = (g.get('start') or '').replace('Z', '+00:00')
        try:
            start = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            start = None
        if start and start > horizon:
            continue
        for side in ('home', 'away'):
            name = g.get(side)
            if name and name not in seen:
                seen.add(name)
                teams.append({'short': name, 'full': name,
                              'kickoff': g.get('start') or ''})
    return teams


def kickoff_readiness(teams: list[dict], now: datetime | None = None) -> list[dict]:
    """Which of these teams would go into a game with a failed or stale feed?"""
    now = now or datetime.now(timezone.utc)
    problems = []
    for team in teams:
        raw = (team.get('kickoff') or '').replace('Z', '+00:00')
        try:
            kickoff = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            kickoff = None
        if not kickoff:
            continue
        hours_out = (kickoff - now).total_seconds() / 3600.0
        if not 0 <= hours_out <= config.KICKOFF_ALERT_HOURS:
            continue
        entry = last_refresh(team_key(team['short'], team.get('full', '')))
        age = age_hours(entry)
        if entry is None or age is None:
            problems.append({'team': team['short'], 'hours_to_kickoff': round(hours_out, 1),
                             'problem': 'never collected'})
        elif not entry.get('ok'):
            problems.append({'team': team['short'], 'hours_to_kickoff': round(hours_out, 1),
                             'problem': f"last refresh FAILED: {(entry.get('error') or '')[:80]}"})
        elif age >= config.INJURY_FEED_TTL_HOURS * 2:
            problems.append({'team': team['short'], 'hours_to_kickoff': round(hours_out, 1),
                             'problem': f'feed is {round(age, 1)}h old'})
    return problems


def scheduled_sweep(now: datetime | None = None) -> dict | None:
    """The game-aware sweep the scheduler runs. Returns a summary, or None if idle.

    Fires only in a configured slot, claims the slot atomically, then refreshes every
    team in an upcoming Top-25 game whose feed has gone stale — and finishes by
    flagging any near-kickoff team the feed is still not ready for.
    """
    if not config.INJURY_SWEEP_ENABLED:
        return None
    now = now or datetime.utcnow()
    if (now.weekday(), now.hour) not in sweep_slots():
        return None
    if not _claim_sweep(now):
        return None

    summary = {'teams': 0, 'swept': 0, 'skipped': 0, 'failed': 0,
               'inserted': 0, 'not_ready': []}
    try:
        teams = slate_teams()
        summary['teams'] = len(teams)
        if teams:
            results = sweep(teams)
            for r in results:
                if r.get('skipped'):
                    summary['skipped'] += 1
                elif r.get('ok'):
                    summary['swept'] += 1
                    summary['inserted'] += r.get('inserted', 0)
                else:
                    summary['failed'] += 1
            summary['not_ready'] = kickoff_readiness(teams)
            for p in summary['not_ready']:
                logging.warning(
                    f"Injury feed NOT READY for {p['team']} "
                    f"({p['hours_to_kickoff']}h to kickoff): {p['problem']}")
        note = (f"{summary['swept']} swept, {summary['skipped']} fresh, "
                f"{summary['failed']} failed of {summary['teams']} teams; "
                f"{summary['inserted']} new rows")
        if summary['not_ready']:
            note += ("; NOT READY: "
                     + ', '.join(p['team'] for p in summary['not_ready']))
        record_refresh(SWEEP_KEY, 'Scheduled injury sweep', not summary['failed']
                       and not summary['not_ready'], summary['teams'],
                       summary['inserted'], error=note[:400])
        logging.info(f"Scheduled injury sweep: {note}")
    except Exception as e:
        logging.exception("Scheduled injury sweep failed")
        record_refresh(SWEEP_KEY, 'Scheduled injury sweep', False, error=str(e)[:400])
        summary['error'] = str(e)
    return summary


# ---------------------------------------------------------------------------
# Freshness reporting
# ---------------------------------------------------------------------------
def fbs_teams(year: int | None = None) -> list[dict]:
    """The FBS team list from CFBD, shaped for sweep(). Raises so the caller can report."""
    import cfbd

    api_key = db.resolve_cfbd_key()
    if not api_key:
        raise RuntimeError("No CFBD key — add a 'CFD' row to API_KEYS.")
    errors: list[dict] = []
    rows = cfbd._get(api_key, '/teams/fbs',
                     {'year': year or cfbd.season_year(datetime.now())},
                     'FBS teams', errors)
    if errors:
        raise RuntimeError(f"CFBD returned HTTP {errors[0]['status']}: "
                           f"{errors[0]['body'][:200]}")
    teams = []
    for row in rows or []:
        school = (row.get('school') or '').strip()
        if not school:
            continue
        mascot = (row.get('mascot') or '').strip()
        teams.append({'short': school, 'full': f'{school} {mascot}'.strip()})
    return teams


def coverage(limit: int = 25) -> dict:
    """Which teams the feed knows about, and how current each one is."""
    out = {'teams': 0, 'fresh': 0, 'stale': 0, 'failed': 0, 'rows': []}
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                f'SELECT team_key, team_name, provider, fetched_at, ok, items, inserted, '
                f'error FROM {REFRESH_TABLE} ORDER BY fetched_at DESC').fetchall() or []
        finally:
            conn.close()
    except Exception as e:
        out['error'] = str(e)
        return out

    for row in rows:
        entry = dict(row)
        if (entry.get('team_key') or '').startswith(SYSTEM_KEY_PREFIX):
            continue          # the sweep claim row is bookkeeping, not a team
        entry['age_hours'] = age_hours(entry)
        out['teams'] += 1
        if not entry['ok']:
            out['failed'] += 1
        elif entry['age_hours'] is not None and entry['age_hours'] < config.INJURY_FEED_TTL_HOURS:
            out['fresh'] += 1
        else:
            out['stale'] += 1
        if len(out['rows']) < limit:
            out['rows'].append(entry)
    return out


def purge(scope: str = 'legacy', older_than: int | None = None,
          dry_run: bool = True, backup: bool = True) -> dict:
    """Delete feed rows. Defaults to a dry run, and backs the file up before writing.

    scope='legacy' removes only rows written before the collector existed — the ones
    with no news_date, which is exactly the old Bright Data scrape output. 'all' removes
    everything. `older_than` further restricts either scope to rows older than N days.

    Deleting rows without clearing the matching feed_refresh entry would leave a team
    marked fresh with nothing behind it, so no report would re-collect it. Any team left
    with no rows therefore loses its cache entry too.
    """
    import os
    import shutil
    import sqlite3

    out = {'scope': scope, 'older_than': older_than, 'dry_run': dry_run,
           'matched': 0, 'deleted': 0, 'total_before': 0, 'total_after': 0,
           'teams_uncached': 0, 'backup': None, 'error': None, 'sample': []}

    if scope not in ('legacy', 'all'):
        out['error'] = "scope must be 'legacy' or 'all'"
        return out

    where, params = [], []
    if scope == 'legacy':
        where.append('news_date IS NULL')
    if older_than is not None:
        cutoff = (datetime.now() - timedelta(days=int(older_than))).date().isoformat()
        # Legacy rows have no news_date to compare, so fall back to parsing date_text
        # in Python for those rather than guessing in SQL.
        where.append('(news_date IS NULL OR news_date < ?)')
        params.append(cutoff)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''

    conn = _connect()
    try:
        out['total_before'] = conn.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]
        rows = conn.execute(
            f'SELECT id, team_name, player_name, headline, date_text, news_date '
            f'FROM {TABLE}{clause}', params).fetchall() or []

        # Apply the age filter to legacy rows here, where date_text can be parsed.
        doomed = []
        for r in rows:
            if older_than is not None and r['news_date'] is None:
                parsed = parse_date_text(r['date_text'])
                if parsed and parsed >= (datetime.now() - timedelta(days=int(older_than))).date():
                    continue
            doomed.append(r)

        out['matched'] = len(doomed)
        out['sample'] = [
            {'team': r['team_name'], 'player': r['player_name'],
             'headline': (r['headline'] or '')[:50], 'date': r['date_text'],
             'collected': r['news_date'] is not None}
            for r in doomed[:10]
        ]

        if dry_run or not doomed:
            out['total_after'] = out['total_before']
            return out

        if backup:
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            path = f"{config.ROTOWIRE_DB_PATH}.backup-{stamp}"
            try:
                target = sqlite3.connect(path)
                try:
                    conn.backup(target)      # consistent snapshot, safe with readers
                finally:
                    target.close()
                out['backup'] = path
                out['backup_bytes'] = os.path.getsize(path)
            except Exception as e:
                out['error'] = (f"Backup failed ({e}); nothing was deleted. "
                                f"Pass backup=False to delete without one.")
                out['total_after'] = out['total_before']
                return out

        ids = [r['id'] for r in doomed]
        with conn:
            for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
                conn.execute(
                    f"DELETE FROM {TABLE} WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk)
            # Drop cache entries for teams that no longer have any rows.
            stale_keys = []
            for entry in conn.execute(
                    f'SELECT team_key, team_name FROM {REFRESH_TABLE}').fetchall() or []:
                remaining = conn.execute(
                    f'SELECT 1 FROM {TABLE} WHERE team_name=? LIMIT 1',
                    (entry['team_name'],)).fetchone()
                if not remaining:
                    stale_keys.append(entry['team_key'])
            for key in stale_keys:
                conn.execute(f'DELETE FROM {REFRESH_TABLE} WHERE team_key=?', (key,))
            out['teams_uncached'] = len(stale_keys)

        out['deleted'] = len(ids)
        out['total_after'] = conn.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]
    except Exception as e:
        out['error'] = f'{e.__class__.__name__}: {e}'
        return out
    finally:
        conn.close()

    # VACUUM cannot run inside a transaction, so it gets its own connection.
    try:
        vac = db.get_rotowire_db_connection()
        try:
            vac.isolation_level = None
            vac.execute('VACUUM')
        finally:
            vac.close()
    except Exception as e:
        logging.warning(f"VACUUM after purge failed (rows are gone regardless): {e}")

    logging.info(f"Purged {out['deleted']} feed rows (scope={scope}); "
                 f"backup at {out['backup']}")
    return out


def status(sample_days: int = 14) -> dict:
    """Freshness of the stored feed. SQLite only — safe on every dashboard paint."""
    import os
    import sqlite3

    path = config.ROTOWIRE_DB_PATH
    out = {
        'path': path, 'exists': os.path.exists(path), 'bytes': None, 'modified': None,
        'rows': 0, 'newest_text': None, 'newest_date': None, 'oldest_date': None,
        'days_stale': None, 'unparseable': 0, 'legacy_unmatched': 0,
        'collected': 0, 'legacy': 0, 'recent': [], 'error': None,
    }
    if not out['exists']:
        out['error'] = f'No feed database at {path}'
        return out
    try:
        stat = os.stat(path)
        out['bytes'] = stat.st_size
        out['modified'] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds')
    except OSError:
        pass

    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            out['rows'] = conn.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]
            has_news_date = any(r[1] == 'news_date'
                                for r in conn.execute(f'PRAGMA table_info({TABLE})'))
            if has_news_date:
                out['collected'] = conn.execute(
                    f'SELECT COUNT(*) FROM {TABLE} WHERE news_date IS NOT NULL'
                ).fetchone()[0]
                out['legacy'] = out['rows'] - out['collected']
                counts = conn.execute(
                    f'SELECT COALESCE(news_date, date_text) AS d, date_text, '
                    f'news_date IS NOT NULL AS collected, COUNT(*) AS n '
                    f'FROM {TABLE} GROUP BY d, date_text, collected').fetchall() or []
            else:
                out['legacy'] = out['rows']
                counts = conn.execute(
                    f'SELECT date_text AS d, date_text, 0 AS collected, COUNT(*) AS n '
                    f'FROM {TABLE} GROUP BY date_text').fetchall() or []
        finally:
            conn.close()
    except Exception as e:
        out['error'] = f'{e.__class__.__name__}: {e}'
        return out

    today = datetime.now().date()
    dated = []
    for row in counts:
        parsed = parse_date_text(row['d'], today) or parse_date_text(row['date_text'], today)
        if parsed is None:
            out['unparseable'] += row['n']
            continue
        # Only legacy rows depend on the exact-string match; collected rows are selected
        # by ISO range, so a non-canonical date_text on those is harmless.
        if not row['collected'] and not matches_report_query(row['date_text']):
            out['legacy_unmatched'] += row['n']
        dated.append((parsed, row['date_text'], row['n'], bool(row['collected'])))

    if dated:
        dated.sort(key=lambda t: t[0])
        out['oldest_date'] = dated[0][0].isoformat()
        newest, newest_text, _, _ = dated[-1]
        out['newest_date'] = newest.isoformat()
        out['newest_text'] = newest_text
        out['days_stale'] = (today - newest).days
        out['recent'] = [
            {'date': d.isoformat(), 'date_text': t, 'rows': n, 'collected': c,
             'readable': c or matches_report_query(t)}
            for d, t, n, c in dated[-sample_days:][::-1]
        ]
    return out


def diagnose() -> dict:
    """status() plus per-team coverage, the OpenRouter key, and a plain-language verdict."""
    report = {'status': status(), 'coverage': coverage(), 'openrouter_key': None,
              'ttl_hours': config.INJURY_FEED_TTL_HOURS, 'verdict': []}

    try:
        report['openrouter_key'] = bool(db.resolve_openrouter_key())
    except Exception as e:
        report['verdict'].append(f'Could not check API_KEYS for the OpenRouter key: {e}')

    st, cov = report['status'], report['coverage']

    if st.get('error'):
        report['verdict'].append(f"The feed database is unreadable: {st['error']}")
    elif not st['rows']:
        report['verdict'].append(
            'The feed is empty. It fills as reports are generated, or run '
            "'admin_tui.py injuries --team \"Georgia\"' to collect one team now.")
    elif st.get('days_stale') is not None and st['days_stale'] > config.ROTOWIRE_STALE_DAYS:
        report['verdict'].append(
            f"The newest item is {st['days_stale']} days old ({st['newest_text']!r}). "
            f"Nothing has been collected since then — generate a report, or sweep.")

    if report['openrouter_key'] is False:
        report['verdict'].append(
            "No OpenRouter key in API_KEYS, so no collection can happen at all. "
            "This alone stops the feed.")

    if st.get('legacy_unmatched'):
        report['verdict'].append(
            f"{st['legacy_unmatched']} rows predate the collector and store a date_text "
            f"the legacy filter cannot match, so reports never saw them. Newly collected "
            f"rows are selected by date range and are not affected.")

    if cov.get('failed'):
        failed = [r['team_name'] for r in cov['rows'] if not r['ok']][:5]
        report['verdict'].append(
            f"{cov['failed']} team refreshes failed, including: {', '.join(failed)}")

    sweep_row = last_refresh(SWEEP_KEY)
    report['last_sweep'] = sweep_row
    if config.INJURY_SWEEP_ENABLED:
        sweep_age = age_hours(sweep_row)
        if sweep_row:
            when = f"{round(sweep_age, 1)}h ago" if sweep_age is not None else 'unknown'
            line = f"Last scheduled sweep {when}: {sweep_row.get('error') or 'ok'}"
            if not sweep_row.get('ok'):
                line += '  <- needs attention'
            report['verdict'].append(line)
        elif sweep_slots():
            report['verdict'].append(
                'The scheduled Top-25 sweep has never run — it fires from the report '
                'scheduler; check that the service is running with SCHEDULES_ENABLED.')

    if not cov.get('teams'):
        report['verdict'].append(
            'No team has been collected yet — this build is the first to record it.')

    if not report['verdict']:
        report['verdict'].append(
            f"The feed is healthy: {st['rows']:,} rows across {cov['teams']} teams, "
            f"{cov['fresh']} of them refreshed within {config.INJURY_FEED_TTL_HOURS}h.")
    return report
