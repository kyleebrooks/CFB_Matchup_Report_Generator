"""Service-wide setting overrides, stored in the database.

Layering, lowest to highest precedence:

    config.default_settings()   env / code defaults
    report_service_settings     service-wide overrides  <- this module
    report_accounts.settings    per-account overrides

Keeping the service-wide layer in the database means the admin console can change the
research or report model without editing /etc/afplna.env and restarting Gunicorn.

Reads are cached briefly and fail soft: if the database is unreachable the service
falls back to the env defaults and keeps generating reports rather than erroring.
"""

import logging
import threading
import time
from datetime import datetime

import config
import db

TABLE = 'report_service_settings'
CACHE_TTL_SECONDS = 60

_lock = threading.Lock()
_cache: dict = {'values': None, 'fetched_at': 0.0, 'error': None}

# Settings that only exist in the environment. Shown read-only in the console so it is
# obvious they need an env edit plus a restart, rather than silently doing nothing.
ENV_ONLY = (
    'DB_HOST', 'DB_USER', 'DB_NAME', 'SERVICE_API_KEY', 'ADMIN_API_KEY',
    'WKHTMLTOPDF_PATH', 'REPORTS_DIR', 'WATERMARKS_DIR', 'ROTOWIRE_DB_PATH',
    'RESEARCH_TIMEOUT', 'REPORT_TIMEOUT', 'CFBD_MAX_WORKERS', 'RESEARCH_MAX_WORKERS',
)


def _coerce(key: str, raw: str):
    """Settings are stored as text; integers come back as integers."""
    if raw is None:
        return None
    if key in ('search_max_results', 'research_max_tokens', 'report_max_tokens',
               # The toggles live in a text column; the string "0" is truthy, so they
               # must come back as integers or "off" would silently mean "on".
               'include_sources', 'include_generation_details'):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return raw


def load(force: bool = False) -> dict:
    """Service-wide overrides. Returns {} when the table is absent or unreachable."""
    with _lock:
        fresh = (time.time() - _cache['fetched_at']) < CACHE_TTL_SECONDS
        if _cache['values'] is not None and fresh and not force:
            return dict(_cache['values'])

    values: dict = {}
    error = None
    try:
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT setting_key, setting_value FROM {TABLE}")
                for key, raw in cur.fetchall() or []:
                    if key not in config.ACCOUNT_SETTING_KEYS:
                        continue          # ignore anything not a recognised setting
                    coerced = _coerce(key, raw)
                    if coerced is not None and coerced != '':
                        values[key] = coerced
        finally:
            conn.close()
    except Exception as e:
        # Missing table on a fresh install is normal and not worth an ERROR log.
        error = str(e)
        logging.debug(f"Service settings unavailable ({e}); using environment defaults.")
        with _lock:
            # Serve a previously good snapshot rather than dropping overrides on a blip.
            if _cache['values'] is not None:
                _cache['error'] = error
                return dict(_cache['values'])
        values = {}

    with _lock:
        _cache['values'] = values
        _cache['fetched_at'] = time.time()
        _cache['error'] = error
    return dict(values)


def resolved_defaults() -> dict:
    """Env defaults with the service-wide overrides applied."""
    resolved = config.default_settings()
    resolved.update(load())
    return resolved


def set_value(key: str, value, updated_by: str = 'admin-console') -> None:
    """Upsert one service-wide override. Validated before it is written."""
    import accounts
    if key not in config.ACCOUNT_SETTING_KEYS:
        raise accounts.AccountError(
            f"Unknown setting '{key}'. Allowed: {', '.join(config.ACCOUNT_SETTING_KEYS)}"
        )
    clean = accounts.validate_settings({key: value})
    if key not in clean:
        raise accounts.AccountError(f"'{key}' resolved to no value")

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE} (setting_key, setting_value, updated_at, updated_by) "
                "VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value), "
                "updated_at=VALUES(updated_at), updated_by=VALUES(updated_by)",
                (key, str(clean[key]), datetime.utcnow(), updated_by),
            )
    finally:
        conn.close()
    invalidate()


def clear_value(key: str) -> None:
    """Drop a service-wide override, reverting to the environment default."""
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {TABLE} WHERE setting_key=%s", (key,))
    finally:
        conn.close()
    invalidate()


def invalidate() -> None:
    with _lock:
        _cache['values'] = None
        _cache['fetched_at'] = 0.0


def describe() -> list[dict]:
    """Every report setting with its value and where that value came from."""
    env = config.default_settings()
    overrides = load()
    out = []
    for key in config.ACCOUNT_SETTING_KEYS:
        overridden = key in overrides
        out.append({
            'key': key,
            'value': overrides.get(key, env.get(key)),
            'env_default': env.get(key),
            'source': 'database' if overridden else 'environment',
            'overridden': overridden,
        })
    return out
