"""Durable backing for the in-process job queue, so builds survive restarts.

The job manager keeps running exactly as before — in memory, one process — but
every submitted job is now mirrored to the report_jobs table: filed on submit,
updated at each stage, closed out on completion. At boot, resume_pending() reloads
whatever was queued or running when the process died and resubmits it under the
SAME job id, so a console that was polling a build straight through a deploy picks
it back up instead of reporting the job lost.

A runner is a closure and cannot be stored, so each row records what is needed to
REBUILD one:
    legacy   the AFPLNA flow — raw params are pipeline.generate kwargs
    report   a typed /v1 or scheduled job — account + report_type + raw request
             params; entitlement, settings, watermark and usage tracking are
             re-derived from the account at resume time, exactly as at submit
    podcast  a voice episode — raw params feed podcasts.generate directly

Rows that cannot be rebuilt (account deactivated, type withdrawn, params no longer
valid) are closed as errors that say so, and their usage rows are closed with them
— an honest "lost to restart" instead of a job that never answers again.

Every write here is best-effort: persistence must never be the reason a report
fails. With the database unreachable, the queue degrades to the old in-memory
behaviour.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import db

TABLE = 'report_jobs'
KEEP_DAYS = 7          # how long finished rows stay queryable across restarts

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    job_id       VARCHAR(20)  NOT NULL PRIMARY KEY,
    kind         VARCHAR(12)  NOT NULL,
    dedup_key    VARCHAR(255) NULL,
    account_id   INT          NULL,
    report_type  VARCHAR(40)  NULL,
    tier         VARCHAR(12)  NULL,
    subject      VARCHAR(190) NULL,
    params_json  MEDIUMTEXT   NOT NULL,
    usage_row_id INT          NULL,
    state        VARCHAR(10)  NOT NULL DEFAULT 'queued',
    stage        VARCHAR(60)  NULL,
    message      VARCHAR(255) NULL,
    percent      SMALLINT     NOT NULL DEFAULT 0,
    home_short   VARCHAR(80)  NULL,
    away_short   VARCHAR(80)  NULL,
    home_full    VARCHAR(120) NULL,
    away_full    VARCHAR(120) NULL,
    result_json  MEDIUMTEXT   NULL,
    error        VARCHAR(255) NULL,
    detail       VARCHAR(500) NULL,
    created_at   DATETIME     NOT NULL,
    finished_at  DATETIME     NULL,
    KEY idx_rjobs_state (state),
    KEY idx_rjobs_account (account_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

COLUMNS = ['job_id', 'kind', 'dedup_key', 'account_id', 'report_type', 'tier',
           'subject', 'params_json', 'usage_row_id', 'state', 'stage', 'message',
           'percent', 'home_short', 'away_short', 'home_full', 'away_full',
           'result_json', 'error', 'detail', 'created_at', 'finished_at']

_schema_ready = False


class ResumeError(RuntimeError):
    """A stored job that cannot be rebuilt, with the reason it cannot."""


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
    finally:
        conn.close()
    _schema_ready = True


def _to_dt(stamp):
    """Job timestamps live as epoch floats in memory and DATETIME in the table."""
    if stamp is None:
        return None
    return datetime.utcfromtimestamp(float(stamp))


def _to_ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).timestamp()
    return float(value)


# ---------------------------------------------------------------------------
# Writes, all fail-soft
# ---------------------------------------------------------------------------
def save_new(job: dict, *, kind: str, raw_params: dict, usage_row_id=None) -> bool:
    """File a freshly submitted job. Returns whether the row landed."""
    try:
        ensure_schema()
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {TABLE} (job_id, kind, dedup_key, account_id, "
                    f"report_type, tier, subject, params_json, usage_row_id, state, "
                    f"stage, message, percent, home_short, away_short, home_full, "
                    f"away_full, created_at) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (job['job_id'], kind, job.get('key'), job.get('account_id'),
                     job.get('report_type'), job.get('tier'),
                     (job.get('subject') or None),
                     json.dumps(raw_params, default=str), usage_row_id,
                     job['state'], job['stage'], job['message'], job['percent'],
                     job.get('home_short'), job.get('away_short'),
                     job.get('home_full'), job.get('away_full'),
                     _to_dt(job['created_at'])))
        finally:
            conn.close()
        return True
    except Exception as e:
        logging.warning(f"Job persistence failed (non-fatal, job runs anyway): {e}")
        return False


_FIELD_MAP = {
    'state': ('state', 10), 'stage': ('stage', 60), 'message': ('message', 255),
    'error': ('error', 255), 'detail': ('detail', 500),
}


def update_fields(job_id: str, fields: dict) -> None:
    """Mirror a job-manager state change onto the row. Unknown keys are ignored."""
    sets, values = [], []
    for key, value in fields.items():
        if key in _FIELD_MAP:
            column, width = _FIELD_MAP[key]
            sets.append(f"{column}=%s")
            values.append(str(value)[:width] if value is not None else None)
        elif key == 'percent':
            sets.append("percent=%s")
            values.append(int(value or 0))
        elif key == 'finished_at':
            sets.append("finished_at=%s")
            values.append(_to_dt(value))
        elif key == 'result':
            sets.append("result_json=%s")
            values.append(json.dumps(value, default=str) if value is not None else None)
    if not sets:
        return
    try:
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {TABLE} SET {', '.join(sets)} WHERE job_id=%s",
                            (*values, job_id))
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"Job mirror update failed (non-fatal): {e}")


def mark_lost(row: dict, reason: str) -> None:
    """Close out a stored job that will never run again, and its usage row."""
    update_fields(row['job_id'], {
        'state': 'error', 'stage': 'error', 'percent': 100,
        'message': f"Not recoverable after restart: {reason}",
        'error': f"Not recoverable after restart: {reason}",
        'finished_at': datetime.utcnow().replace(tzinfo=timezone.utc).timestamp(),
    })
    if row.get('usage_row_id'):
        try:
            import usage
            usage.mark_complete(row['usage_row_id'], 'error',
                                error=f"lost to service restart: {reason}"[:255])
        except Exception as e:
            logging.debug(f"Usage close-out failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _job_view(row: dict) -> dict:
    """A stored row in the exact shape the job manager hands out."""
    result = None
    if row.get('result_json'):
        try:
            result = json.loads(row['result_json'])
        except ValueError:
            result = None
    return {
        'job_id': row['job_id'], 'key': row.get('dedup_key'),
        'state': row['state'], 'stage': row.get('stage') or row['state'],
        'message': row.get('message') or '', 'percent': row.get('percent') or 0,
        'home_short': row.get('home_short'), 'away_short': row.get('away_short'),
        'home_full': row.get('home_full'), 'away_full': row.get('away_full'),
        'created_at': _to_ts(row['created_at']),
        'finished_at': _to_ts(row.get('finished_at')),
        'result': result, 'error': row.get('error'), 'detail': row.get('detail'),
        'account_id': row.get('account_id'), 'report_type': row.get('report_type'),
        'tier': row.get('tier'), 'subject': row.get('subject'),
    }


def _rows(where: str, params: tuple) -> list[dict]:
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} {where}", params)
            return [dict(zip(COLUMNS, r)) for r in cur.fetchall() or []]
    finally:
        conn.close()


def load(job_id: str) -> dict | None:
    """The stored job, in job-manager shape — how a poll outlives a restart."""
    try:
        rows = _rows("WHERE job_id=%s", (job_id,))
        return _job_view(rows[0]) if rows else None
    except Exception as e:
        logging.debug(f"Job lookup fell back to memory only ({e})")
        return None


def latest_for_key(dedup_key: str) -> dict | None:
    try:
        rows = _rows("WHERE dedup_key=%s ORDER BY created_at DESC LIMIT 1",
                     (dedup_key,))
        return _job_view(rows[0]) if rows else None
    except Exception as e:
        logging.debug(f"Job key lookup fell back to memory only ({e})")
        return None


def for_account(account_id: int, limit: int = 100) -> list[dict]:
    try:
        rows = _rows("WHERE account_id=%s ORDER BY created_at DESC LIMIT %s",
                     (int(account_id), int(limit)))
        return [_job_view(r) for r in rows]
    except Exception as e:
        logging.debug(f"Account job history unavailable ({e})")
        return []


# ---------------------------------------------------------------------------
# Resume at boot
# ---------------------------------------------------------------------------
def _build_report_runner(row: dict, raw: dict):
    """Recreate a typed report job the way /v1/reports and the scheduler build one."""
    import accounts
    import report_types
    import reports_store
    import usage

    account = accounts.get(row['account_id']) if row.get('account_id') else None
    if not account or not account.get('active'):
        raise ResumeError('account missing or inactive')
    rtype = row.get('report_type') or ''
    if rtype not in (account.get('allowed_reports') or []):
        raise ResumeError(f"account no longer entitled to {rtype}")
    try:
        spec = report_types.get(rtype)
        params = spec['validate'](raw)
    except report_types.ValidationError as e:
        raise ResumeError(str(e))

    params['settings'] = accounts.effective_settings(account)
    tier = (row.get('tier') or 'standard').lower()
    if tier == 'premium':
        params['settings'] = dict(params['settings'],
                                  report_model=params['settings']['premium_report_model'])
    params['tier'] = tier
    params['watermark'] = accounts.watermark_path(account)
    params['report_dir'] = reports_store.account_dir(account['id'])
    params['account_id'] = account['id']

    usage_row = row.get('usage_row_id')

    def tracked(job_params, progress, _spec=spec, _row=usage_row):
        try:
            result = _spec['run'](job_params, progress)
        except Exception as e:
            usage.mark_complete(_row, 'error', error=f"{e.__class__.__name__}: {e}")
            raise
        usage.mark_complete(_row, 'done', seconds=result.get('seconds'),
                            sources=result.get('sources'))
        return result

    return params, tracked


def _build_podcast_runner(row: dict, raw: dict):
    import podcasts
    import usage

    usage_row = row.get('usage_row_id')

    def tracked(job_params, progress, _row=usage_row):
        try:
            result = podcasts.generate(
                script=job_params['script'], tts_model=job_params['tts_model'],
                voice=job_params.get('voice'),
                clone_audio=job_params.get('clone_audio'),
                clone_transcript=job_params.get('clone_transcript'),
                title=job_params.get('title'),
                account_id=job_params['account_id'], progress=progress)
        except Exception as e:
            usage.mark_complete(_row, 'error', error=f"{e.__class__.__name__}: {e}")
            raise
        usage.mark_complete(_row, 'done')
        return result

    return raw, tracked


def _rebuild(row: dict):
    """(params, runner) for one stored job, or ResumeError explaining why not."""
    try:
        raw = json.loads(row['params_json'])
    except ValueError:
        raise ResumeError('stored parameters are unreadable')
    kind = row.get('kind')
    if kind == 'legacy':
        return raw, None                    # the manager's default pipeline runner
    if kind == 'report':
        return _build_report_runner(row, raw)
    if kind == 'podcast':
        return _build_podcast_runner(row, raw)
    raise ResumeError(f"unknown job kind '{kind}'")


def resume_pending(manager) -> dict:
    """Reload interrupted jobs into the manager. Called once at boot.

    Each rebuilt job keeps its job id, so clients polling across the restart
    reconnect to the same handle. Jobs that cannot be rebuilt are closed as
    errors that say why. Old finished rows are purged on the way through.
    """
    summary = {'resumed': 0, 'lost': 0, 'purged': 0}
    try:
        ensure_schema()
        rows = _rows("WHERE state IN ('queued','running') ORDER BY created_at ASC", ())
    except Exception as e:
        logging.warning(f"Job resume skipped — queue store unreachable: {e}")
        return summary

    for row in rows:
        try:
            params, runner = _rebuild(row)
        except ResumeError as e:
            mark_lost(row, str(e))
            summary['lost'] += 1
            logging.warning(f"Job {row['job_id']} ({row.get('subject') or 'legacy'}) "
                            f"not resumed: {e}")
            continue
        except Exception as e:
            mark_lost(row, f"{e.__class__.__name__}: {e}")
            summary['lost'] += 1
            logging.exception(f"Job {row['job_id']} rebuild crashed")
            continue
        meta = {k: row[k] for k in ('account_id', 'report_type', 'tier', 'subject')
                if row.get(k) is not None}
        job = manager.submit(params, runner=runner, key=row.get('dedup_key'),
                             meta=meta, resume_job_id=row['job_id'])
        if job.get('deduplicated'):
            # A newer submission already owns this dedup key; this row's build will
            # never run, so close it rather than leave it queued forever.
            mark_lost(row, 'superseded by a newer submission for the same subject')
            summary['lost'] += 1
        else:
            summary['resumed'] += 1

    try:
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {TABLE} WHERE state IN ('done','error') "
                    f"AND created_at < %s",
                    (datetime.utcnow() - timedelta(days=KEEP_DAYS),))
                summary['purged'] = cur.rowcount or 0
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"Job row purge skipped ({e})")

    if summary['resumed'] or summary['lost']:
        logging.info(f"Job queue resume: {summary['resumed']} resumed, "
                     f"{summary['lost']} closed as lost, "
                     f"{summary['purged']} old rows purged.")
    return summary
