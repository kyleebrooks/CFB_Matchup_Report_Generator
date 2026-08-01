"""Per-account API usage tracking.

One row per report request, written when the job is queued and updated when it
finishes. Cheap enough to be unconditional, and it survives restarts — unlike the
in-memory job table, which only knows about the current service lifetime.

Every write is best-effort: usage accounting must never be the reason a customer's
report fails.
"""

import logging
from datetime import datetime, timedelta

import db

TABLE = 'report_usage'


def record_request(account_id: int, report_type: str, subject: str, job_id: str) -> int | None:
    """Log a queued request. Returns the row id, or None if the write failed."""
    try:
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {TABLE} (account_id, report_type, subject, job_id, state, "
                    "created_at) VALUES (%s,%s,%s,%s,'queued',%s)",
                    (account_id, report_type, (subject or '')[:190], job_id, datetime.utcnow()),
                )
                return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"Usage record failed (non-fatal): {e}")
        return None


def attach_job(row_id: int | None, job_id: str) -> None:
    """Link a usage row to its job once the job manager has assigned an id."""
    if not row_id:
        return
    try:
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {TABLE} SET job_id=%s WHERE id=%s", (job_id, row_id))
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"Usage job link failed (non-fatal): {e}")


def mark_complete(row_id: int | None, state: str, seconds=None, sources=None,
                  error: str = '') -> None:
    if not row_id:
        return
    try:
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {TABLE} SET state=%s, seconds=%s, sources=%s, error=%s, "
                    "completed_at=%s WHERE id=%s",
                    (state, seconds, sources, (error or '')[:255], datetime.utcnow(), row_id),
                )
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"Usage completion update failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def summary_by_account() -> dict:
    """{account_id: {total, done, error, last_30d, last_used, by_type}}"""
    out: dict[int, dict] = {}
    cutoff = datetime.utcnow() - timedelta(days=30)
    try:
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT account_id, report_type, state, created_at FROM {TABLE}"
                )
                for account_id, report_type, state, created in cur.fetchall() or []:
                    entry = out.setdefault(account_id, {
                        'total': 0, 'done': 0, 'error': 0, 'running': 0,
                        'last_30d': 0, 'last_used': None, 'by_type': {},
                    })
                    entry['total'] += 1
                    if state == 'done':
                        entry['done'] += 1
                    elif state == 'error':
                        entry['error'] += 1
                    else:
                        entry['running'] += 1
                    entry['by_type'][report_type] = entry['by_type'].get(report_type, 0) + 1

                    stamp = created
                    if isinstance(stamp, str):
                        try:
                            stamp = datetime.fromisoformat(stamp)
                        except ValueError:
                            stamp = None
                    if stamp:
                        if stamp >= cutoff:
                            entry['last_30d'] += 1
                        if not entry['last_used'] or stamp > entry['last_used']:
                            entry['last_used'] = stamp
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"Usage summary unavailable: {e}")
    return out


def recent(account_id: int | None = None, limit: int = 30) -> list[dict]:
    """Most recent requests, newest first. All accounts when account_id is None."""
    rows: list[dict] = []
    try:
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                sql = (f"SELECT id, account_id, report_type, subject, state, seconds, sources, "
                       f"created_at, error FROM {TABLE}")
                params: tuple = ()
                if account_id is not None:
                    sql += " WHERE account_id=%s"
                    params = (account_id,)
                sql += " ORDER BY id DESC LIMIT %s"
                params = params + (int(limit),)
                cur.execute(sql, params)
                for r in cur.fetchall() or []:
                    rows.append({
                        'id': r[0], 'account_id': r[1], 'report_type': r[2], 'subject': r[3],
                        'state': r[4], 'seconds': r[5], 'sources': r[6],
                        'created_at': r[7], 'error': r[8],
                    })
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"Usage history unavailable: {e}")
    return rows


def totals() -> dict:
    summary = summary_by_account()
    return {
        'accounts_with_usage': len(summary),
        'requests': sum(s['total'] for s in summary.values()),
        'done': sum(s['done'] for s in summary.values()),
        'errors': sum(s['error'] for s in summary.values()),
        'last_30d': sum(s['last_30d'] for s in summary.values()),
    }
