"""The Rotowire injury/news feed: the scheduled Bright Data scrape and its diagnostics.

This lived inline in app.py, where every failure path was a `logging.error` followed by
a bare `return`, and the outer `except Exception` swallowed everything else. Nothing was
persisted and nothing was surfaced, so a job that failed twice a day could keep failing
for a year without leaving a trace anywhere an operator would look — `/health` called
the table "ok" purely because `COUNT(*)` succeeded against it.

So the rule here is: a run always records its outcome, and the outcome always names the
stage that failed and what the vendor actually said. `diagnose()` then reads that back
alongside the state of the data and says, in words, why the feed is stale.
"""

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta

import requests

import config
import db

COLLECTOR_ID = os.getenv('BRIGHT_COLLECTOR_ID', 'c_meewnv1y2gctpr239v')
TRIGGER_URL = 'https://api.brightdata.com/dca/trigger'
DATASET_URL = 'https://api.brightdata.com/dca/dataset'

RUNS_TABLE = 'rotowire_runs'
FIELDS = ('player_name', 'headline', 'team_name', 'date_text',
          'news_text', 'source_name', 'position', 'analysis_text')

# Bright Data answers the dataset endpoint while a snapshot is still building. Those
# replies are JSON objects, not the finished array.
BUILDING_STATES = {'building', 'running', 'collecting', 'pending', 'scheduled', 'queued'}


# ---------------------------------------------------------------------------
# Run history — kept in the Rotowire SQLite file so a MySQL outage cannot stop
# the scrape from recording why it failed.
# ---------------------------------------------------------------------------
def ensure_runs_table(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at   TEXT,
            finished_at  TEXT,
            ok           INTEGER,
            stage        TEXT,
            fetched      INTEGER,
            inserted     INTEGER,
            duplicates   INTEGER,
            seconds      REAL,
            http_status  INTEGER,
            error        TEXT,
            detail       TEXT
        )
        """
    )


def record_run(result: dict) -> None:
    """Persist a run outcome. Never raises — this is diagnostics, not the product."""
    try:
        conn = db.get_rotowire_db_connection()
        try:
            with conn:
                ensure_runs_table(conn)
                conn.execute(
                    f"INSERT INTO {RUNS_TABLE} (started_at, finished_at, ok, stage, fetched, "
                    "inserted, duplicates, seconds, http_status, error, detail) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        result.get('started_at'), result.get('finished_at'),
                        1 if result.get('ok') else 0, result.get('stage'),
                        result.get('fetched'), result.get('inserted'),
                        result.get('duplicates'), result.get('seconds'),
                        result.get('http_status'), (result.get('error') or '')[:500],
                        (result.get('detail') or '')[:2000],
                    ),
                )
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"Could not record the Rotowire run (non-fatal): {e}")


def recent_runs(limit: int = 10) -> list[dict]:
    try:
        conn = db.get_rotowire_db_connection()
        try:
            ensure_runs_table(conn)
            cur = conn.execute(
                f"SELECT started_at, ok, stage, fetched, inserted, duplicates, seconds, "
                f"http_status, error FROM {RUNS_TABLE} ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in cur.fetchall() or []]
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"Rotowire run history unavailable: {e}")
        return []


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
# The first entry is the one that matters: db.format_friendly_date() produces exactly
# this, and the report query matches date_text by string equality against it. If the
# collector ever starts writing a different shape, the feed goes silently empty even
# though the scrape is working — so the diagnostic checks the shape explicitly.
EXPECTED_FORMAT = '%B %-d, %Y'
_PARSE_FORMATS = (
    '%B %d, %Y', '%b %d, %Y', '%B %d %Y', '%b %d %Y',
    '%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%d %B %Y', '%b. %d, %Y',
)
_RELATIVE = re.compile(r'^(\d+)\s*(minute|min|hour|hr|day|week)s?\s+ago$')


def parse_date_text(text: str, today=None):
    """Best-effort parse of whatever the collector wrote. None when unrecognised."""
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
    """True when this stored value is one the report's date filter could ever match."""
    parsed = parse_date_text(text)
    if parsed is None:
        return False
    return db.format_friendly_date(datetime.combine(parsed, datetime.min.time())) == \
        (text or '').strip()


# ---------------------------------------------------------------------------
# The scrape
# ---------------------------------------------------------------------------
def _payload_rows(payload):
    """Normalise a dataset reply into (rows, not_ready_reason).

    The old code accepted anything truthy here. A 200 carrying `{"status":"building"}`
    is truthy, so it broke out of the poll and then iterated the dict — yielding its
    string keys — and died on `str.get`. That crash looks identical every run and is
    invisible outside the journal, which is exactly how a feed stops for a year.
    """
    if isinstance(payload, list):
        rows = [r for r in payload if isinstance(r, dict)]
        return rows, None if rows else 'the snapshot came back as an empty list'
    if isinstance(payload, dict):
        for key in ('data', 'records', 'results', 'items'):
            if isinstance(payload.get(key), list):
                rows = [r for r in payload[key] if isinstance(r, dict)]
                return rows, None if rows else f"'{key}' was present but empty"
        state = str(payload.get('status') or payload.get('state') or '').lower()
        if state in BUILDING_STATES:
            return None, f"snapshot still building (status={state!r})"
        if payload.get('error') or payload.get('message'):
            return [], f"vendor said: {payload.get('error') or payload.get('message')}"
        return None, f"unrecognised object reply with keys {sorted(payload)[:6]}"
    return [], f'unexpected payload type {type(payload).__name__}'


def scrape(bright_key: str | None = None, *, poll_seconds: int = 720,
           dry_run: bool = False, log=logging.info) -> dict:
    """Run one collection. Returns a structured result; never raises.

    Every early exit names its stage, so a caller (and the run history) can say which
    step failed rather than just "it didn't work".
    """
    started = datetime.utcnow()
    began = time.time()
    result = {
        'ok': False, 'stage': 'start', 'started_at': started.isoformat(timespec='seconds'),
        'finished_at': None, 'fetched': 0, 'inserted': 0, 'duplicates': 0,
        'seconds': 0.0, 'http_status': None, 'error': None, 'detail': None,
        'collector': COLLECTOR_ID, 'dry_run': dry_run,
    }

    def done(stage, error=None, detail=None, ok=False, http_status=None):
        result.update(stage=stage, ok=ok, error=error, detail=detail,
                      seconds=round(time.time() - began, 1),
                      finished_at=datetime.utcnow().isoformat(timespec='seconds'))
        if http_status is not None:
            result['http_status'] = http_status
        if error:
            log(f"Rotowire scrape failed at '{stage}': {error}")
        return result

    # -- 1. credentials ---------------------------------------------------
    try:
        bright_key = bright_key or db.get_api_key('bright')
    except Exception as e:
        return done('key', f"Could not read the API_KEYS table: {e}")
    if not bright_key:
        return done('key', "No 'bright' row in API_KEYS (or its value is empty).",
                    "Add it with: INSERT INTO API_KEYS (API_NAME, `KEY`) "
                    "VALUES ('bright', '<bright-data-api-token>');")

    # -- 2. trigger -------------------------------------------------------
    try:
        trig = requests.post(
            f"{TRIGGER_URL}?queue_next=1&collector={COLLECTOR_ID}",
            json=[{}],
            headers={'Authorization': f'Bearer {bright_key}',
                     'Content-Type': 'application/json'},
            timeout=30,
        )
    except requests.RequestException as e:
        return done('trigger', f"Could not reach Bright Data: {e.__class__.__name__}: {e}")

    result['http_status'] = trig.status_code
    if trig.status_code != 200:
        hint = {
            401: 'the API token is rejected — rotate it in the Bright Data console',
            403: 'the token is valid but not entitled to this collector, or the '
                 'account is suspended / out of credit',
            404: f'collector {COLLECTOR_ID} no longer exists on this account',
        }.get(trig.status_code, '')
        return done('trigger', f"Trigger returned HTTP {trig.status_code}"
                               f"{' — ' + hint if hint else ''}",
                    trig.text[:1000], http_status=trig.status_code)

    try:
        data = trig.json()
    except ValueError:
        return done('trigger', 'Trigger returned a 200 that was not JSON.',
                    trig.text[:1000])
    # The docs describe collection_id and snapshot_id as the same value under two names.
    collection_id = (data or {}).get('collection_id') or (data or {}).get('snapshot_id')
    if not collection_id:
        return done('trigger', 'Trigger succeeded but returned no collection_id.',
                    json.dumps(data)[:1000])
    log(f"Rotowire collection {collection_id} triggered")

    if dry_run:
        return done('trigger', ok=True, detail=f'dry run — collection {collection_id} '
                                               f'triggered, not polled or inserted')

    # -- 3. poll ----------------------------------------------------------
    deadline = time.time() + poll_seconds
    rows, last_reason, delay = None, 'never polled', 2.0
    while time.time() < deadline:
        try:
            resp = requests.get(f"{DATASET_URL}?id={collection_id}",
                                headers={'Authorization': f'Bearer {bright_key}'},
                                timeout=30)
        except requests.RequestException as e:
            last_reason = f'{e.__class__.__name__}: {e}'
            time.sleep(delay)
            continue

        result['http_status'] = resp.status_code
        if resp.status_code == 202 or not resp.text.strip():
            last_reason = f'HTTP {resp.status_code}, empty body — still building'
        elif resp.status_code != 200:
            last_reason = f'HTTP {resp.status_code}: {resp.text[:200]}'
        else:
            try:
                payload = resp.json()
            except ValueError:
                # NDJSON: one JSON object per line.
                try:
                    payload = [json.loads(ln) for ln in resp.text.splitlines() if ln.strip()]
                except ValueError as e:
                    last_reason = f'reply parsed as neither JSON nor NDJSON: {e}'
                    payload = None
            if payload is not None:
                rows, reason = _payload_rows(payload)
                if rows is not None:
                    last_reason = reason or ''
                    break
                last_reason = reason

        # Back off rather than hammering the endpoint once a second for 12 minutes.
        time.sleep(delay)
        delay = min(delay * 1.5, 20.0)

    if rows is None:
        return done('poll', f'Snapshot never became available within {poll_seconds}s.',
                    last_reason)
    if not rows:
        return done('poll', 'The snapshot completed but contained no usable records.',
                    last_reason or 'the collector returned zero rows — it may no longer '
                                   'match the Rotowire page layout')

    result['fetched'] = len(rows)

    # -- 4. insert --------------------------------------------------------
    try:
        conn = db.get_rotowire_db_connection()
    except Exception as e:
        return done('insert', f"Could not open {config.ROTOWIRE_DB_PATH}: {e}")
    try:
        inserted = duplicates = 0
        with conn:
            cur = conn.cursor()
            for entry in rows:
                values = tuple((entry.get(f) or '').strip() for f in FIELDS)
                cur.execute(
                    f"SELECT 1 FROM rotowire WHERE "
                    + " AND ".join(f"{f}=?" for f in FIELDS) + " LIMIT 1", values)
                if cur.fetchone():
                    duplicates += 1
                    continue
                cur.execute(
                    f"INSERT INTO rotowire ({','.join(FIELDS)}) "
                    f"VALUES ({','.join('?' * len(FIELDS))})", values)
                inserted += 1
            cur.close()
    except Exception as e:
        return done('insert', f'{e.__class__.__name__}: {e}')
    finally:
        conn.close()

    result.update(inserted=inserted, duplicates=duplicates)
    log(f"Rotowire scrape completed: {len(rows)} fetched, {inserted} new, "
        f"{duplicates} already present")
    return done('complete', ok=True,
                detail=f'{len(rows)} fetched, {inserted} inserted, {duplicates} duplicates')


def run_and_record(**kwargs) -> dict:
    result = scrape(**kwargs)
    record_run(result)
    return result


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def status(sample_days: int = 14) -> dict:
    """Freshness of the local feed. SQLite only — safe to call on every dashboard paint."""
    path = config.ROTOWIRE_DB_PATH
    out = {
        'path': path, 'exists': os.path.exists(path), 'bytes': None, 'modified': None,
        'rows': 0, 'newest_text': None, 'newest_date': None, 'oldest_date': None,
        'days_stale': None, 'unparseable': 0, 'mismatched_format': 0,
        'recent': [], 'error': None,
    }
    if not out['exists']:
        out['error'] = f'No Rotowire database at {path}'
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
            out['rows'] = conn.execute('SELECT COUNT(*) FROM rotowire').fetchone()[0]
            counts = conn.execute(
                'SELECT date_text, COUNT(*) AS n FROM rotowire GROUP BY date_text'
            ).fetchall() or []
        finally:
            conn.close()
    except Exception as e:
        out['error'] = f'{e.__class__.__name__}: {e}'
        return out

    today = datetime.now().date()
    dated = []
    for row in counts:
        parsed = parse_date_text(row['date_text'], today)
        if parsed is None:
            out['unparseable'] += row['n']
            continue
        if not matches_report_query(row['date_text']):
            out['mismatched_format'] += row['n']
        dated.append((parsed, row['date_text'], row['n']))

    if dated:
        dated.sort(key=lambda t: t[0])
        out['oldest_date'] = dated[0][0].isoformat()
        newest, newest_text, _ = dated[-1]
        out['newest_date'] = newest.isoformat()
        out['newest_text'] = newest_text
        out['days_stale'] = (today - newest).days
        out['recent'] = [
            {'date': d.isoformat(), 'date_text': t, 'rows': n,
             'matches_report_query': matches_report_query(t)}
            for d, t, n in dated[-sample_days:][::-1]
        ]
    return out


def schedule() -> list[dict]:
    """What the scheduler will actually do, with the timezone it will really use."""
    from apscheduler.triggers.cron import CronTrigger
    out = []
    for hour in SCRAPE_HOURS:
        trigger = CronTrigger(hour=hour, minute=0, timezone=config.SCHEDULER_TIMEZONE)
        nxt = trigger.get_next_fire_time(None, datetime.now(trigger.timezone))
        out.append({
            'hour': hour,
            'timezone': str(trigger.timezone),
            'next_run': nxt.isoformat(timespec='seconds') if nxt else None,
        })
    return out


SCRAPE_HOURS = (9, 18)


def diagnose() -> dict:
    """status() plus everything that needs MySQL or the scheduler, and a verdict."""
    report = {'status': status(), 'runs': recent_runs(10), 'schedule': [],
              'bright_key': None, 'verdict': []}

    try:
        report['schedule'] = schedule()
    except Exception as e:
        report['verdict'].append(f'Could not resolve the schedule: {e}')

    try:
        report['bright_key'] = bool(db.get_api_key('bright'))
    except Exception as e:
        report['bright_key'] = None
        report['verdict'].append(
            f'Could not check API_KEYS for the Bright Data token: {e}')

    st = report['status']
    stale = st.get('days_stale')

    if st.get('error'):
        report['verdict'].append(f"The feed database is unreadable: {st['error']}")
    elif not st['rows']:
        report['verdict'].append('The feed table is empty — no scrape has ever inserted a row.')
    elif stale is None:
        report['verdict'].append(
            'No row has a parseable date — the collector is writing a date_text shape '
            'nothing here recognises.')
    elif stale > config.ROTOWIRE_STALE_DAYS:
        report['verdict'].append(
            f"The newest row is {stale} days old ({st['newest_text']!r}). The scrape has "
            f"not added anything since then.")

    if report['bright_key'] is False:
        report['verdict'].append(
            "There is no 'bright' row in API_KEYS, so the scrape aborts at its first "
            "step every single run. This alone stops the feed.")

    if st.get('mismatched_format'):
        report['verdict'].append(
            f"{st['mismatched_format']} rows store a date_text the report query can never "
            f"match — it filters on exact strings like "
            f"{db.format_friendly_date(datetime.now())!r}. Even a working scrape would "
            f"read back as an empty feed.")

    failed = [r for r in report['runs'] if not r['ok']]
    if report['runs'] and failed and failed[0] is report['runs'][0]:
        last = report['runs'][0]
        report['verdict'].append(
            f"The last recorded run failed at stage '{last['stage']}': {last['error']}")
    if not report['runs']:
        report['verdict'].append(
            'No run history yet — this build is the first to record one. Run '
            "'admin_tui.py rotowire --run' to capture the real failure.")

    if not report['verdict']:
        report['verdict'].append('The feed looks healthy and current.')
    return report
