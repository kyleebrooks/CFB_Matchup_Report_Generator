"""Voice jobs: episodes rendered by a VibeVoice studio on someone else's machine.

The podcasts module synthesizes audio here, on the droplet, through OpenRouter. This
module is for the other case: the account owns a GPU workstation running VibeVoice, and
wants *that* to render the episode — multi-speaker, cloned voices, effects — while the
episode still lands on the site like any other.

That machine sits behind a home NAT, so nothing here can call it. The direction is
inverted: the workstation polls this queue, claims a job, renders it, and posts the audio
back. This service never opens a connection to the studio, never learns its address, and
does not care when it disappears for a week.

    site ──POST /v1/voice-jobs──► [queued] ◄──GET /v1/voice-jobs/next── studio
                                     │                                    │
                                     ├──── PATCH …/<id> (progress) ◄──────┤
                                     └──── POST …/<id>/audio ◄────────────┘
                                                │
                                                └─► podcasts.store_upload() → published

A claim carries a **lease**. If the workstation is rebooted, loses power or crashes
mid-render, the lease expires and the job returns to `queued` for the next poll rather
than sitting `running` forever. This is the only recovery mechanism the queue needs: jobs
are idempotent to re-run, because nothing is published until the audio actually arrives.
"""

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta

import config
import db

TABLE = 'voice_jobs'
WORKERS_TABLE = 'voice_workers'

# Same ceiling the OpenRouter path uses — a script this long is already a feature-length
# episode, and the cap is really there to stop a runaway paste.
MAX_SCRIPT_CHARS = 100_000

# States. A job is only ever picked up from 'queued'; everything else is terminal or owned
# by a worker holding a live lease.
QUEUED, RUNNING, DONE, ERROR, CANCELED = 'queued', 'running', 'done', 'error', 'canceled'
ACTIVE_STATES = (QUEUED, RUNNING)

# How long a worker may hold a job without saying anything. Generous: a long episode on a
# busy GPU can spend many minutes inside one stage, and reaping a job that is genuinely
# still rendering would mean rendering it twice.
LEASE_SECONDS = int(getattr(config, 'VOICE_JOB_LEASE_SECONDS', 900))

# A studio that has not been heard from in this long is shown as offline to the console.
WORKER_ONLINE_SECONDS = 180


class VoiceJobError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    account_id    INT          NOT NULL,
    state         VARCHAR(16)  NOT NULL DEFAULT 'queued',
    title         VARCHAR(200) NOT NULL,
    script        MEDIUMTEXT   NOT NULL,
    automation    VARCHAR(64)  DEFAULT NULL,
    vibevoice_model VARCHAR(120) DEFAULT NULL,
    preset        VARCHAR(64)  DEFAULT NULL,
    speakers      TEXT         DEFAULT NULL,
    stage         VARCHAR(200) DEFAULT NULL,
    percent       INT          NOT NULL DEFAULT 0,
    worker_id     VARCHAR(64)  DEFAULT NULL,
    claim_token   VARCHAR(36)  DEFAULT NULL,
    lease_expires DATETIME     DEFAULT NULL,
    attempts      INT          NOT NULL DEFAULT 0,
    episode       INT          DEFAULT NULL,
    filename      VARCHAR(255) DEFAULT NULL,
    error         VARCHAR(500) DEFAULT NULL,
    created_at    DATETIME     NOT NULL,
    updated_at    DATETIME     NOT NULL,
    KEY idx_voice_job_account (account_id, id),
    KEY idx_voice_job_state (state, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_WORKERS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {WORKERS_TABLE} (
    worker_id  VARCHAR(64)  NOT NULL PRIMARY KEY,
    account_id INT          DEFAULT NULL,
    label      VARCHAR(120) DEFAULT NULL,
    catalog    MEDIUMTEXT   DEFAULT NULL,
    busy       TINYINT(1)   NOT NULL DEFAULT 0,
    last_seen  DATETIME     NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
            cur.execute(_WORKERS_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _schema_ready = True


_FIELDS = ('id', 'account_id', 'state', 'title', 'script', 'automation',
           'vibevoice_model', 'preset', 'speakers', 'stage', 'percent',
           'worker_id', 'lease_expires', 'attempts', 'episode', 'filename',
           'error', 'created_at', 'updated_at')
_SELECT = ', '.join(_FIELDS)


def _row(r: tuple, *, include_script: bool = False) -> dict:
    out = dict(zip(_FIELDS, r))
    try:
        out['speakers'] = json.loads(out['speakers']) if out['speakers'] else {}
    except (TypeError, ValueError):
        out['speakers'] = {}
    for key in ('created_at', 'updated_at', 'lease_expires'):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat(sep=' ', timespec='seconds')
    if not include_script:
        # The console polls status every couple of seconds; shipping the whole
        # script back each time is pure waste.
        out['script_chars'] = len(out.pop('script') or '')
    return out


# ---------------------------------------------------------------------------
# Producing side — the console
# ---------------------------------------------------------------------------
def enqueue(*, account_id: int, title: str, script: str,
            automation: str | None = None, vibevoice_model: str | None = None,
            preset: str | None = None, speakers: dict | None = None) -> dict:
    """Queue one episode for the account's studio to render."""
    ensure_schema()
    script = (script or '').strip()
    if not script:
        raise VoiceJobError('The episode script is empty.')
    if len(script) > MAX_SCRIPT_CHARS:
        raise VoiceJobError(
            f'Script is {len(script)} characters (limit {MAX_SCRIPT_CHARS}).', 413)
    title = (title or '').strip()[:200]

    now = datetime.now()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO {TABLE} (account_id, state, title, script, automation, '
                f'vibevoice_model, preset, speakers, stage, created_at, updated_at) '
                f'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                (int(account_id), QUEUED, title, script, automation or None,
                 vibevoice_model or None, preset or None,
                 json.dumps(speakers or {}), 'Waiting for the studio', now, now))
            job_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    logging.info(f'Voice job {job_id} queued for account {account_id}: {title!r} '
                 f'({len(script)} chars)')
    return get(account_id, job_id)


def get(account_id: int, job_id: int, *, include_script: bool = False) -> dict:
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {_SELECT} FROM {TABLE} WHERE id=%s AND account_id=%s',
                        (int(job_id), int(account_id)))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise VoiceJobError('No such voice job.', 404)
    return _row(row, include_script=include_script)


def list_for(account_id: int, limit: int = 20) -> list[dict]:
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {_SELECT} FROM {TABLE} WHERE account_id=%s '
                        f'ORDER BY id DESC LIMIT %s', (int(account_id), int(limit)))
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return [_row(r) for r in rows]


def cancel(account_id: int, job_id: int) -> dict:
    """Give up on a job. A worker already rendering it will be told to stop at its
    next progress report."""
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE {TABLE} SET state=%s, stage=%s, updated_at=%s '
                f'WHERE id=%s AND account_id=%s AND state IN %s',
                (CANCELED, 'Canceled', datetime.now(), int(job_id), int(account_id),
                 ACTIVE_STATES))
        conn.commit()
    finally:
        conn.close()
    return get(account_id, job_id)


# ---------------------------------------------------------------------------
# Consuming side — the studio worker
# ---------------------------------------------------------------------------
def reap_expired() -> int:
    """Return jobs whose worker went quiet to the queue. Safe to call on every poll.

    A worker that died mid-render leaves a job `running` with a lease in the past.
    Nothing was published — publication only happens when audio arrives — so the job can
    simply be handed to the next worker that asks.
    """
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE {TABLE} SET state=%s, worker_id=NULL, claim_token=NULL, '
                f'lease_expires=NULL, stage=%s, percent=0, updated_at=%s '
                f'WHERE state=%s AND lease_expires IS NOT NULL AND lease_expires < %s',
                (QUEUED, 'Returned to the queue — the studio went quiet',
                 datetime.now(), RUNNING, datetime.now()))
            freed = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if freed:
        logging.warning(f'{freed} voice job(s) reclaimed after a lease expired')
    return freed


def claim_next(worker_id: str, *, account_id: int | None = None) -> dict | None:
    """Atomically take the oldest queued job, or None.

    Two workers polling at the same instant must never get the same job. The UPDATE
    stamps a token unique to this attempt and MySQL serializes the row lock; the
    follow-up SELECT then reads back exactly the row this call won — which a plain
    `WHERE worker_id=…` could not guarantee if the same worker claimed twice.
    """
    ensure_schema()
    reap_expired()
    token = uuid.uuid4().hex
    now = datetime.now()
    expires = now + timedelta(seconds=LEASE_SECONDS)

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            params = [RUNNING, worker_id[:64], token, expires, 'Claimed by the studio',
                      now, QUEUED]
            sql = (f'UPDATE {TABLE} SET state=%s, worker_id=%s, claim_token=%s, '
                   f'lease_expires=%s, stage=%s, updated_at=%s, attempts=attempts+1 '
                   f'WHERE state=%s')
            if account_id is not None:
                sql += ' AND account_id=%s'
                params.append(int(account_id))
            sql += ' ORDER BY id LIMIT 1'
            cur.execute(sql, params)
            if not cur.rowcount:
                return None
            cur.execute(f'SELECT {_SELECT} FROM {TABLE} WHERE claim_token=%s', (token,))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        return None
    job = _row(row, include_script=True)
    logging.info(f'Voice job {job["id"]} claimed by {worker_id}')
    return job


def progress(job_id: int, *, worker_id: str, stage: str | None = None,
             percent: int | None = None) -> dict:
    """Record a progress report and extend the lease.

    Returns the job so the worker can see a cancellation without a second call.
    """
    ensure_schema()
    now = datetime.now()
    sets = ['updated_at=%s', 'lease_expires=%s']
    params: list = [now, now + timedelta(seconds=LEASE_SECONDS)]
    if stage is not None:
        sets.append('stage=%s')
        params.append(str(stage)[:200])
    if percent is not None:
        sets.append('percent=%s')
        params.append(max(0, min(100, int(percent))))
    params += [int(job_id), worker_id[:64]]

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'UPDATE {TABLE} SET {", ".join(sets)} '
                        f'WHERE id=%s AND worker_id=%s', params)
            cur.execute(f'SELECT {_SELECT} FROM {TABLE} WHERE id=%s', (int(job_id),))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        raise VoiceJobError('No such voice job.', 404)
    return _row(row)


def claimed_by(job_id: int, worker_id: str) -> dict:
    """The job a worker is holding, script included, or 404/409."""
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {_SELECT} FROM {TABLE} WHERE id=%s', (int(job_id),))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise VoiceJobError('No such voice job.', 404)
    job = _row(row, include_script=True)
    if job['worker_id'] != worker_id:
        raise VoiceJobError('That job is not held by this worker.', 409)
    return job


def complete(job_id: int, *, worker_id: str, episode: int, filename: str) -> dict:
    ensure_schema()
    now = datetime.now()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE {TABLE} SET state=%s, stage=%s, percent=100, episode=%s, '
                f'filename=%s, error=NULL, lease_expires=NULL, updated_at=%s '
                f'WHERE id=%s AND worker_id=%s',
                (DONE, 'Published', int(episode), filename, now,
                 int(job_id), worker_id))
            # No match means the lease was reaped and someone else holds the job now.
            # Saying "published" here would strand a second render nobody asked for.
            if not cur.rowcount:
                raise VoiceJobError('That job is no longer held by this worker.', 409)
            cur.execute(f'SELECT {_SELECT} FROM {TABLE} WHERE id=%s', (int(job_id),))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    logging.info(f'Voice job {job_id} published as episode {episode} ({filename})')
    return _row(row)


def fail(job_id: int, *, worker_id: str, error: str) -> dict:
    ensure_schema()
    now = datetime.now()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE {TABLE} SET state=%s, stage=%s, error=%s, lease_expires=NULL, '
                f'updated_at=%s WHERE id=%s AND worker_id=%s',
                (ERROR, 'Failed', str(error)[:500], now, int(job_id), worker_id))
            cur.execute(f'SELECT {_SELECT} FROM {TABLE} WHERE id=%s', (int(job_id),))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    logging.error(f'Voice job {job_id} failed: {error}')
    if not row:
        raise VoiceJobError('No such voice job.', 404)
    return _row(row)


# ---------------------------------------------------------------------------
# Worker registry — what the console shows in its voice pickers
# ---------------------------------------------------------------------------
def heartbeat(worker_id: str, *, account_id: int | None = None,
              label: str | None = None, catalog: dict | None = None,
              busy: bool = False) -> dict:
    """Record that a studio is alive, and what it can do.

    The catalog — voice profiles, presets, models — originates on the workstation and is
    cached here so the console can populate its pickers without ever talking to it.
    """
    ensure_schema()
    now = datetime.now()
    payload = json.dumps(catalog) if catalog is not None else None
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            # An empty heartbeat (no catalog) must not wipe a good one — a worker that
            # cannot reach its own app still reports in, and the console should keep
            # showing the voices it knew about.
            if payload is None:
                cur.execute(
                    f'INSERT INTO {WORKERS_TABLE} (worker_id, account_id, label, busy, '
                    f'last_seen) VALUES (%s, %s, %s, %s, %s) '
                    f'ON DUPLICATE KEY UPDATE account_id=VALUES(account_id), '
                    f'label=VALUES(label), busy=VALUES(busy), last_seen=VALUES(last_seen)',
                    (worker_id[:64], account_id, (label or '')[:120] or None,
                     1 if busy else 0, now))
            else:
                cur.execute(
                    f'INSERT INTO {WORKERS_TABLE} (worker_id, account_id, label, catalog, '
                    f'busy, last_seen) VALUES (%s, %s, %s, %s, %s, %s) '
                    f'ON DUPLICATE KEY UPDATE account_id=VALUES(account_id), '
                    f'label=VALUES(label), catalog=VALUES(catalog), busy=VALUES(busy), '
                    f'last_seen=VALUES(last_seen)',
                    (worker_id[:64], account_id, (label or '')[:120] or None, payload,
                     1 if busy else 0, now))
        conn.commit()
    finally:
        conn.close()
    return {'ok': True, 'worker_id': worker_id, 'at': now.isoformat(sep=' ',
                                                                   timespec='seconds')}


def studio_status(account_id: int) -> dict:
    """Whether this account has a studio online, and what it offers.

    Never raises for the ordinary "no studio has ever checked in" case — the console
    renders an offline badge from this and must not 500 because the workstation is off.
    """
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT worker_id, label, catalog, busy, last_seen FROM {WORKERS_TABLE} '
                f'WHERE account_id=%s OR account_id IS NULL '
                f'ORDER BY last_seen DESC LIMIT 1', (int(account_id),))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {'online': False, 'catalog': {}, 'worker_id': None, 'last_seen': None}

    worker_id, label, catalog, busy, last_seen = row
    age = (datetime.now() - last_seen).total_seconds() if last_seen else 1e9
    try:
        parsed = json.loads(catalog) if catalog else {}
    except (TypeError, ValueError):
        parsed = {}
    return {
        'online': age <= WORKER_ONLINE_SECONDS,
        'busy': bool(busy),
        'worker_id': worker_id,
        'label': label,
        'catalog': parsed,
        'last_seen': last_seen.isoformat(sep=' ', timespec='seconds') if last_seen else None,
        'seconds_ago': int(age),
    }
