"""Recurring report schedules, per account. Optional, and off by default.

A schedule says: for THIS account, generate THIS report type every week at THIS
day/hour (UTC), scoped to THESE games. The runner thread wakes every minute, claims
due schedules atomically in the database (so multiple workers never double-fire), and
submits ordinary jobs — the same path a manual API call takes, with the same
entitlement check, usage accounting, per-account settings and watermark.

Scopes, for the game-driven types (matchup, full_game_recap):
    top25         either team in the latest AP Top 25
    all           every FBS game in the window
    team:X        games involving school X
    conference:Y  games where either side is in conference Y

The runner also carries the housekeeping tick: grading pending predictions once the
games behind them have finished.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import db

TABLE = 'report_schedules'
VALID_TYPES = ('weekly_preview', 'weekly_wrap', 'matchup', 'full_game_recap',
               'prediction_audit', 'prediction_review')
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    account_id INT NOT NULL,
    report_type VARCHAR(40) NOT NULL,
    scope VARCHAR(120) NOT NULL DEFAULT 'top25',
    day_of_week TINYINT NOT NULL DEFAULT 1,
    hour_utc TINYINT NOT NULL DEFAULT 12,
    max_reports INT NOT NULL DEFAULT 10,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    last_run_at DATETIME NULL,
    last_result VARCHAR(255) NULL,
    created_at DATETIME NOT NULL,
    KEY idx_sched_due (enabled, day_of_week, hour_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

COLUMNS = ['id', 'account_id', 'report_type', 'scope', 'day_of_week', 'hour_utc',
           'max_reports', 'enabled', 'last_run_at', 'last_result', 'created_at']


class ScheduleError(ValueError):
    pass


def ensure_schema() -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
    finally:
        conn.close()


def _validate_scope(report_type: str, scope: str) -> str:
    scope = (scope or 'top25').strip()
    if report_type in ('weekly_preview', 'weekly_wrap',
                       'prediction_audit', 'prediction_review'):
        return '-'                    # these are one report; scope does not apply
    low = scope.lower()
    if low in ('top25', 'all'):
        return low
    if low.startswith('team:') and scope[5:].strip():
        return f"team:{scope[5:].strip()}"
    if low.startswith('conference:') and scope[11:].strip():
        return f"conference:{scope[11:].strip()}"
    raise ScheduleError(
        "scope must be 'top25', 'all', 'team:<school>' or 'conference:<name>'")


def create(account_id: int, report_type: str, scope: str = 'top25',
           day_of_week: int = 1, hour_utc: int = 12, max_reports: int = 10) -> dict:
    report_type = (report_type or '').strip().lower()
    if report_type not in VALID_TYPES:
        raise ScheduleError(f"report_type must be one of: {', '.join(VALID_TYPES)}")
    scope = _validate_scope(report_type, scope)
    if not 0 <= int(day_of_week) <= 6:
        raise ScheduleError("day_of_week must be 0 (Mon) through 6 (Sun)")
    if not 0 <= int(hour_utc) <= 23:
        raise ScheduleError("hour_utc must be 0 through 23")
    if not 1 <= int(max_reports) <= 30:
        raise ScheduleError("max_reports must be between 1 and 30")

    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE} (account_id, report_type, scope, day_of_week, "
                f"hour_utc, max_reports, enabled, created_at) "
                f"VALUES (%s,%s,%s,%s,%s,%s,1,%s)",
                (int(account_id), report_type, scope, int(day_of_week),
                 int(hour_utc), int(max_reports), datetime.utcnow()))
            new_id = cur.lastrowid
    finally:
        conn.close()
    return get(new_id)


def list_all() -> list[dict]:
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "
                        f"ORDER BY account_id, day_of_week, hour_utc")
            return [dict(zip(COLUMNS, r)) for r in cur.fetchall() or []]
    finally:
        conn.close()


def get(schedule_id: int) -> dict | None:
    for row in list_all():
        if row['id'] == schedule_id:
            return row
    return None


def update(schedule_id: int, *, scope=None, day_of_week=None, hour_utc=None,
           max_reports=None) -> dict:
    """Adjust a schedule in place. Only the supplied fields change."""
    row = get(schedule_id)
    if not row:
        raise ScheduleError('No such schedule')
    scope = _validate_scope(row['report_type'],
                            scope if scope is not None else row['scope'])
    day = int(day_of_week if day_of_week is not None else row['day_of_week'])
    hour = int(hour_utc if hour_utc is not None else row['hour_utc'])
    cap = int(max_reports if max_reports is not None else row['max_reports'])
    if not 0 <= day <= 6:
        raise ScheduleError("day_of_week must be 0 (Mon) through 6 (Sun)")
    if not 0 <= hour <= 23:
        raise ScheduleError("hour_utc must be 0 through 23")
    if not 1 <= cap <= 30:
        raise ScheduleError("max_reports must be between 1 and 30")
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE} SET scope=%s, day_of_week=%s, hour_utc=%s, "
                f"max_reports=%s WHERE id=%s",
                (scope, day, hour, cap, int(schedule_id)))
    finally:
        conn.close()
    return get(schedule_id)


def set_enabled(schedule_id: int, enabled: bool) -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {TABLE} SET enabled=%s WHERE id=%s",
                        (1 if enabled else 0, int(schedule_id)))
    finally:
        conn.close()


def delete(schedule_id: int) -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {TABLE} WHERE id=%s", (int(schedule_id),))
    finally:
        conn.close()


def _mark_ran(schedule_id: int, result: str) -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {TABLE} SET last_result=%s WHERE id=%s",
                        (result[:250], int(schedule_id)))
    finally:
        conn.close()


def claim_due(now: datetime | None = None) -> list[dict]:
    """Atomically claim every schedule due this hour.

    The claim is the UPDATE of last_run_at guarded by its previous value: whichever
    worker's UPDATE lands first wins the row, so two gunicorn workers ticking in the
    same minute cannot both fire the same schedule.
    """
    now = now or datetime.utcnow()
    claimed = []
    for row in list_all():
        if not row['enabled']:
            continue
        if int(row['day_of_week']) != now.weekday() or int(row['hour_utc']) != now.hour:
            continue
        last = row.get('last_run_at')
        if last is not None:
            if isinstance(last, str):
                try:
                    last = datetime.fromisoformat(last)
                except ValueError:
                    last = None
            if last is not None and (now - last).total_seconds() < 20 * 3600:
                continue                       # already ran this week-slot
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                if row.get('last_run_at') is None:
                    cur.execute(
                        f"UPDATE {TABLE} SET last_run_at=%s "
                        f"WHERE id=%s AND last_run_at IS NULL",
                        (now, row['id']))
                else:
                    cur.execute(
                        f"UPDATE {TABLE} SET last_run_at=%s "
                        f"WHERE id=%s AND last_run_at=%s",
                        (now, row['id'], row['last_run_at']))
                won = cur.rowcount == 1
        finally:
            conn.close()
        if won:
            claimed.append(row)
    return claimed


# ---------------------------------------------------------------------------
# Turning a claimed schedule into jobs
# ---------------------------------------------------------------------------
def _scoped_games(rows: list[dict], scope: str, top25: dict, limit: int) -> list[dict]:
    def keep(g):
        if scope == 'all':
            return True
        if scope == 'top25':
            return g['home'] in top25 or g['away'] in top25
        if scope.startswith('team:'):
            team = scope[5:]
            return team in (g['home'], g['away'])
        if scope.startswith('conference:'):
            conf = scope[11:].lower()
            return conf in ((g.get('home_conference') or '').lower(),
                            (g.get('away_conference') or '').lower())
        return False

    picked = [g for g in rows if keep(g)]
    # Most consequential first when a cap bites: ranked involvement, then earliest.
    picked.sort(key=lambda g: (0 if (g['home'] in top25 or g['away'] in top25) else 1,
                               g['start'] or ''))
    return picked[:limit]


def _submit(account: dict, report_type: str, raw_params: dict) -> dict:
    """Submit one job exactly the way /v1/reports does, marked as scheduled."""
    import accounts
    import jobs
    import report_types
    import reports_store
    import usage

    if report_type not in (account.get('allowed_reports') or []):
        return {'skipped': f"not entitled to {report_type}"}

    spec = report_types.get(report_type)
    params = spec['validate'](raw_params)
    params['settings'] = accounts.effective_settings(account)
    params['watermark'] = accounts.watermark_path(account)
    params['report_dir'] = reports_store.account_dir(account['id'])
    params['account_id'] = account['id']
    subject = spec['subject'](params)
    usage_row = usage.record_request(account['id'], report_type, subject, 'scheduled')

    def tracked(job_params, progress, _spec=spec, _row=usage_row):
        try:
            result = _spec['run'](job_params, progress)
        except Exception as e:
            usage.mark_complete(_row, 'error', error=f"{e.__class__.__name__}: {e}")
            raise
        usage.mark_complete(_row, 'done', seconds=result.get('seconds'),
                            sources=result.get('sources'))
        return result

    job = jobs.manager.submit(
        params, runner=tracked,
        key=f"acct{account['id']}:{spec['dedup_key'](params)}",
        meta={'account_id': account['id'], 'report_type': report_type,
              'subject': subject, 'scheduled': True})
    usage.attach_job(usage_row, job['job_id'])
    if job.get('deduplicated'):
        usage.mark_complete(usage_row, 'duplicate')
    return {'job_id': job['job_id'], 'subject': subject}


def run_schedule(row: dict) -> str:
    """Execute one claimed schedule. Returns a one-line result summary."""
    import accounts
    import cfbd
    import db as db_mod
    import weekly

    account = accounts.get(row['account_id'])
    if not account or not account.get('active'):
        return 'skipped: account missing or inactive'

    rtype = row['report_type']
    if rtype in ('weekly_preview', 'weekly_wrap', 'prediction_audit',
                 'prediction_review'):
        out = _submit(account, rtype, {})
        return out.get('skipped') or f"submitted 1 job ({out.get('subject')})"

    api_key = db_mod.resolve_cfbd_key()
    if not api_key:
        return 'skipped: no CFBD key'
    completed = rtype == 'full_game_recap'
    data = weekly.build_week_data(api_key, completed=completed)
    games = _scoped_games(data['games'], row['scope'], data['top25'],
                          int(row['max_reports']))
    if not games:
        return f"no games matched scope '{row['scope']}'"

    submitted, skipped = 0, 0
    for g in games:
        if rtype == 'matchup':
            raw = {'home_full': g['home'], 'away_full': g['away'],
                   'home_short': g['home'], 'away_short': g['away']}
        else:
            raw = {'game_id': g['game_id']}
        out = _submit(account, rtype, raw)
        if out.get('skipped'):
            skipped += 1
        else:
            submitted += 1
    note = f"submitted {submitted} of {len(games)} matched games"
    if skipped:
        note += f" ({skipped} skipped)"
    return note


# ---------------------------------------------------------------------------
# The runner thread
# ---------------------------------------------------------------------------
def tick(now: datetime | None = None) -> list[str]:
    """One pass: fire due schedules, then grade pending predictions. Fail-soft."""
    results = []
    try:
        for row in claim_due(now):
            try:
                note = run_schedule(row)
            except Exception as e:
                logging.exception(f"Schedule {row['id']} failed")
                note = f"error: {e}"
            _mark_ran(row['id'], note)
            logging.info(f"Schedule {row['id']} ({row['report_type']} for account "
                         f"{row['account_id']}): {note}")
            results.append(note)
    except Exception as e:
        logging.debug(f"Schedule tick skipped ({e})")

    # Housekeeping: grade whatever finished since the last pass, at most hourly.
    global _last_grade
    stamp = time.monotonic()
    if stamp - _last_grade > 3600:
        _last_grade = stamp
        try:
            import db as db_mod
            import predictions
            key = db_mod.resolve_cfbd_key()
            if key:
                predictions.grade_pending(key)
        except Exception as e:
            logging.debug(f"Grading pass skipped ({e})")
    return results


_last_grade = 0.0


def start() -> None:
    """Run the scheduler loop in a daemon thread. Safe under multiple workers —
    the database claim decides who fires."""
    def loop():
        while True:
            time.sleep(60)
            try:
                tick(datetime.utcnow())
            except Exception:
                logging.exception("Scheduler tick crashed; continuing")

    threading.Thread(target=loop, daemon=True, name='schedules').start()
