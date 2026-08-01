"""Multi-tenant API accounts, stored in the shared MySQL database.

Each account owns an API key, the set of report types it may request, per-account
model/search settings, and an optional watermark image. The legacy AFPLNA flow
(SERVICE_API_KEY on /generate-report) is untouched by any of this and keeps working
exactly as before.

Keys are stored as SHA-256 hashes, never plaintext: a full key is shown once, at
creation or rotation, and cannot be recovered afterwards.
"""

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime

import config
import db

TABLE = 'report_accounts'

KEY_PREFIX = 'cfbr_'
PREFIX_DISPLAY_LEN = 12


class AccountError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    account_name    VARCHAR(120)  NOT NULL,
    contact_email   VARCHAR(190)  DEFAULT NULL,
    api_key_hash    CHAR(64)      NOT NULL,
    api_key_prefix  VARCHAR(24)   NOT NULL,
    is_admin        TINYINT(1)    NOT NULL DEFAULT 0,
    active          TINYINT(1)    NOT NULL DEFAULT 1,
    allowed_reports TEXT          DEFAULT NULL,
    settings        TEXT          DEFAULT NULL,
    watermark_file  VARCHAR(255)  DEFAULT NULL,
    created_at      DATETIME      NOT NULL,
    updated_at      DATETIME      NOT NULL,
    UNIQUE KEY uniq_api_key_hash (api_key_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_schema_ready = False


def ensure_schema() -> None:
    """Create the accounts table if it does not exist. Safe to call repeatedly."""
    global _schema_ready
    if _schema_ready:
        return
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
        _schema_ready = True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
def hash_key(api_key: str) -> str:
    return hashlib.sha256((api_key or '').strip().encode('utf-8')).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def key_prefix(api_key: str) -> str:
    return (api_key or '')[:PREFIX_DISPLAY_LEN]


# ---------------------------------------------------------------------------
# Row <-> dict
# ---------------------------------------------------------------------------
def _loads(value, fallback):
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return fallback
    return parsed


def _row_to_account(row: tuple) -> dict:
    (
        acc_id, name, email, _hash, prefix, is_admin, active,
        allowed, settings, watermark, created, updated,
    ) = row
    return {
        'id': acc_id,
        'account_name': name,
        'contact_email': email,
        'api_key_prefix': prefix,
        'is_admin': bool(is_admin),
        'active': bool(active),
        'allowed_reports': _loads(allowed, []),
        'settings': _loads(settings, {}),
        'watermark_file': watermark,
        'created_at': created.isoformat() if hasattr(created, 'isoformat') else str(created),
        'updated_at': updated.isoformat() if hasattr(updated, 'isoformat') else str(updated),
    }


_SELECT = (
    "SELECT id, account_name, contact_email, api_key_hash, api_key_prefix, is_admin, "
    f"active, allowed_reports, settings, watermark_file, created_at, updated_at FROM {TABLE}"
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def find_by_key(api_key: str) -> dict | None:
    """Resolve an API key to an account. Returns None for unknown or disabled keys."""
    if not api_key or not str(api_key).strip():
        return None
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT + " WHERE api_key_hash=%s LIMIT 1", (hash_key(api_key),))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    account = _row_to_account(row)
    return account if account['active'] else None


def get(account_id: int) -> dict | None:
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT + " WHERE id=%s LIMIT 1", (account_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    return _row_to_account(row) if row else None


def list_all(include_inactive: bool = True) -> list[dict]:
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            sql = _SELECT + ("" if include_inactive else " WHERE active=1") + " ORDER BY id"
            cur.execute(sql)
            rows = cur.fetchall() or []
    finally:
        conn.close()
    return [_row_to_account(r) for r in rows]


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
def create(
    account_name: str,
    allowed_reports: list[str],
    *,
    contact_email: str | None = None,
    settings: dict | None = None,
    is_admin: bool = False,
) -> tuple[dict, str]:
    """Create an account. Returns (account, plaintext_api_key).

    The plaintext key is returned once and never stored; only its hash is persisted.
    """
    if not account_name or not str(account_name).strip():
        raise AccountError("account_name is required")

    ensure_schema()
    api_key = generate_key()
    now = datetime.utcnow()
    clean_settings = validate_settings(settings or {})

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE} (account_name, contact_email, api_key_hash, api_key_prefix, "
                "is_admin, active, allowed_reports, settings, watermark_file, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,1,%s,%s,NULL,%s,%s)",
                (
                    str(account_name).strip(),
                    (contact_email or '').strip() or None,
                    hash_key(api_key),
                    key_prefix(api_key),
                    1 if is_admin else 0,
                    json.dumps(list(allowed_reports or [])),
                    json.dumps(clean_settings),
                    now, now,
                ),
            )
            account_id = cur.lastrowid
    finally:
        conn.close()

    logging.info(f"Created API account {account_id} ({account_name}), admin={is_admin}")
    return get(account_id), api_key


def update(account_id: int, **fields) -> dict:
    """Update mutable account columns. Unknown fields are rejected."""
    ensure_schema()
    sets, params = [], []

    if 'account_name' in fields:
        sets.append("account_name=%s")
        params.append(str(fields['account_name']).strip())
    if 'contact_email' in fields:
        sets.append("contact_email=%s")
        params.append((fields['contact_email'] or '').strip() or None)
    if 'active' in fields:
        sets.append("active=%s")
        params.append(1 if fields['active'] else 0)
    if 'is_admin' in fields:
        sets.append("is_admin=%s")
        params.append(1 if fields['is_admin'] else 0)
    if 'allowed_reports' in fields:
        sets.append("allowed_reports=%s")
        params.append(json.dumps(list(fields['allowed_reports'] or [])))
    if 'settings' in fields:
        sets.append("settings=%s")
        params.append(json.dumps(validate_settings(fields['settings'] or {})))
    if 'watermark_file' in fields:
        sets.append("watermark_file=%s")
        params.append(fields['watermark_file'])

    if not sets:
        raise AccountError("No updatable fields supplied")

    sets.append("updated_at=%s")
    params.append(datetime.utcnow())
    params.append(account_id)

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {TABLE} SET {', '.join(sets)} WHERE id=%s", params)
    finally:
        conn.close()

    account = get(account_id)
    if not account:
        raise AccountError("Account not found", 404)
    return account


def merge_settings(account_id: int, patch: dict) -> dict:
    """Apply a partial settings update, leaving unmentioned keys alone."""
    account = get(account_id)
    if not account:
        raise AccountError("Account not found", 404)
    merged = dict(account['settings'])
    for key, value in (patch or {}).items():
        if value is None:
            merged.pop(key, None)      # null clears an override, back to the default
        else:
            merged[key] = value
    return update(account_id, settings=merged)


def delete(account_id: int, purge_usage: bool = False) -> dict:
    """Permanently remove an account.

    The REST API only deactivates, which preserves history; the admin console can
    delete outright. Usage rows are kept by default so billing history survives an
    account being removed — pass purge_usage=True to drop them too.
    """
    account = get(account_id)
    if not account:
        raise AccountError("Account not found", 404)

    if account.get('watermark_file'):
        path = os.path.join(config.WATERMARKS_DIR, account['watermark_file'])
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                logging.warning(f"Could not delete watermark {path}: {e}")

    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            if purge_usage:
                try:
                    cur.execute("DELETE FROM report_usage WHERE account_id=%s", (account_id,))
                except Exception as e:
                    logging.warning(f"Could not purge usage rows: {e}")
            cur.execute(f"DELETE FROM {TABLE} WHERE id=%s", (account_id,))
    finally:
        conn.close()

    logging.info(f"Deleted API account {account_id} ({account['account_name']})")
    return account


def rotate_key(account_id: int) -> tuple[dict, str]:
    """Issue a new key for an account, invalidating the old one immediately."""
    ensure_schema()
    if not get(account_id):
        raise AccountError("Account not found", 404)
    api_key = generate_key()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE} SET api_key_hash=%s, api_key_prefix=%s, updated_at=%s WHERE id=%s",
                (hash_key(api_key), key_prefix(api_key), datetime.utcnow(), account_id),
            )
    finally:
        conn.close()
    logging.info(f"Rotated API key for account {account_id}")
    return get(account_id), api_key


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
_EFFORTS = ('low', 'medium', 'high')
_ENGINES = ('exa', 'native', '')
_INT_BOUNDS = {
    'search_max_results': (1, 25),          # OpenRouter's documented Exa range
    'research_max_tokens': (1000, 200000),
    'report_max_tokens': (2000, 400000),
}


def validate_settings(settings: dict) -> dict:
    """Reject unknown or out-of-range settings rather than silently dropping them."""
    if not isinstance(settings, dict):
        raise AccountError("settings must be an object")

    clean: dict = {}
    for key, value in settings.items():
        if key not in config.ACCOUNT_SETTING_KEYS:
            raise AccountError(
                f"Unknown setting '{key}'. Allowed: {', '.join(config.ACCOUNT_SETTING_KEYS)}"
            )
        if value is None:
            continue

        if key in ('research_model', 'report_model'):
            model = str(value).strip()
            if '/' not in model:
                raise AccountError(
                    f"'{key}' must be a full OpenRouter model id such as "
                    f"'deepseek/deepseek-v4-flash' (got '{model}')"
                )
            if config.ALLOWED_ACCOUNT_MODELS and model not in config.ALLOWED_ACCOUNT_MODELS:
                raise AccountError(
                    f"Model '{model}' is not permitted. Allowed: "
                    f"{', '.join(config.ALLOWED_ACCOUNT_MODELS)}"
                )
            clean[key] = model

        elif key == 'search_engine':
            engine = str(value).strip().lower()
            if engine not in _ENGINES:
                raise AccountError("search_engine must be 'exa', 'native' or '' (auto)")
            clean[key] = engine

        elif key in ('research_effort', 'report_effort'):
            effort = str(value).strip().lower()
            if effort not in _EFFORTS:
                raise AccountError(f"'{key}' must be one of: {', '.join(_EFFORTS)}")
            clean[key] = effort

        else:
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise AccountError(f"'{key}' must be an integer")
            low, high = _INT_BOUNDS[key]
            if not low <= number <= high:
                raise AccountError(f"'{key}' must be between {low} and {high}")
            clean[key] = number

    return clean


def effective_settings(account: dict | None) -> dict:
    """Resolve the three settings layers, lowest precedence first.

        config.default_settings()   environment / code defaults
        report_service_settings     service-wide overrides (admin console)
        account['settings']         this account's overrides

    Imported lazily: settings_store reaches back into this module to validate, and a
    top-level import both ways would be a cycle.
    """
    try:
        import settings_store
        resolved = settings_store.resolved_defaults()
    except Exception:
        resolved = config.default_settings()
    if account:
        resolved.update(account.get('settings') or {})
    return resolved


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------
def watermark_path(account: dict | None) -> str:
    """Absolute path to the watermark for this account, or the AFPLNA default."""
    if account and account.get('watermark_file'):
        path = os.path.join(config.WATERMARKS_DIR, account['watermark_file'])
        if os.path.exists(path):
            return path
        logging.warning(
            f"Account {account.get('id')} watermark {account['watermark_file']} is missing; "
            f"falling back to the default."
        )
    return config.WATERMARK_PATH


def save_watermark(account_id: int, data: bytes, content_type: str = '') -> dict:
    """Validate and store an uploaded watermark, then point the account at it."""
    if not data:
        raise AccountError("No image data received")
    if len(data) > config.MAX_WATERMARK_BYTES:
        raise AccountError(
            f"Image is {len(data)} bytes; the limit is {config.MAX_WATERMARK_BYTES}"
        )

    # Verify it really is a decodable image before it can break a PDF render later.
    try:
        import io
        from reportlab.lib.utils import ImageReader
        probe = ImageReader(io.BytesIO(data))
        width, height = probe.getSize()
        if not width or not height:
            raise ValueError("zero-sized image")
    except Exception as e:
        raise AccountError(f"Not a usable image ({e.__class__.__name__}: {e})")

    suffix = {'image/jpeg': '.jpg', 'image/webp': '.webp'}.get(
        (content_type or '').lower().split(';')[0].strip(), '.png'
    )
    os.makedirs(config.WATERMARKS_DIR, exist_ok=True)
    filename = f"account_{account_id}{suffix}"
    path = os.path.join(config.WATERMARKS_DIR, filename)

    tmp = path + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(data)
    os.replace(tmp, path)

    # Drop any previous file with a different extension so stale images cannot linger.
    for other in ('.png', '.jpg', '.webp'):
        if other != suffix:
            stale = os.path.join(config.WATERMARKS_DIR, f"account_{account_id}{other}")
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass

    account = update(account_id, watermark_file=filename)
    logging.info(f"Stored watermark for account {account_id}: {filename} ({len(data)} bytes)")
    return {'account': account, 'filename': filename, 'bytes': len(data),
            'width': width, 'height': height}


def clear_watermark(account_id: int) -> dict:
    account = get(account_id)
    if account and account.get('watermark_file'):
        path = os.path.join(config.WATERMARKS_DIR, account['watermark_file'])
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                logging.warning(f"Could not delete watermark {path}: {e}")
    return update(account_id, watermark_file=None)


# ---------------------------------------------------------------------------
# Admin bootstrap
# ---------------------------------------------------------------------------
def is_admin_key(api_key: str) -> bool:
    """True for the env bootstrap key, or for any account flagged is_admin."""
    if not api_key:
        return False
    if config.ADMIN_API_KEY and secrets.compare_digest(str(api_key), str(config.ADMIN_API_KEY)):
        return True
    account = find_by_key(api_key)
    return bool(account and account['is_admin'])


def public_view(account: dict, include_settings: bool = True) -> dict:
    """Account as returned by the API. Never includes key material beyond the prefix."""
    out = {
        'id': account['id'],
        'account_name': account['account_name'],
        'contact_email': account['contact_email'],
        'api_key_prefix': account['api_key_prefix'],
        'active': account['active'],
        'is_admin': account['is_admin'],
        'allowed_reports': account['allowed_reports'],
        'has_custom_watermark': bool(account.get('watermark_file')),
        'created_at': account['created_at'],
        'updated_at': account['updated_at'],
    }
    if include_settings:
        out['settings'] = account['settings']
        out['effective_settings'] = effective_settings(account)
    return out
