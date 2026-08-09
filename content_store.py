"""Editable site content, stored per account.

The public site runs on an ephemeral container — anything written to its disk is
gone on the next deploy — so page copy an admin edits from the console has to live
somewhere durable. It lives here, next to the accounts that own it: one row per
(account, key), plain text, no history. The site reads it with a fallback baked
into its templates, so this store being unreachable can never blank a page.
"""

import logging
import re
from datetime import datetime

import db

TABLE = 'site_content'
MAX_CONTENT_BYTES = 20_000
_KEY_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    account_id  INT          NOT NULL,
    content_key VARCHAR(64)  NOT NULL,
    content     TEXT         DEFAULT NULL,
    updated_at  DATETIME     NOT NULL,
    PRIMARY KEY (account_id, content_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class ContentError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _check_key(key: str) -> str:
    key = (key or '').strip().lower()
    if not _KEY_RE.match(key):
        raise ContentError(
            "Content key must be 1-64 characters of lowercase letters, digits, "
            "hyphens or underscores.")
    return key


_schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _schema_ready = True


def get(account_id: int, key: str) -> dict:
    """One content entry, or {'content': None} when nothing is stored yet."""
    key = _check_key(key)
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT content, updated_at FROM {TABLE} "
                f"WHERE account_id=%s AND content_key=%s",
                (int(account_id), key))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {'key': key, 'content': None, 'updated_at': None}
    return {'key': key, 'content': row[0], 'updated_at': row[1]}


def put(account_id: int, key: str, content: str) -> dict:
    """Store one entry, replacing whatever was there."""
    key = _check_key(key)
    if not isinstance(content, str):
        raise ContentError("'content' must be a string.")
    if len(content.encode('utf-8')) > MAX_CONTENT_BYTES:
        raise ContentError(
            f"Content is too large ({len(content.encode('utf-8'))} bytes; the "
            f"limit is {MAX_CONTENT_BYTES}).", 413)

    now = datetime.now()
    ensure_schema()
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TABLE} (account_id, content_key, content, updated_at) "
                f"VALUES (%s, %s, %s, %s) "
                f"ON DUPLICATE KEY UPDATE content=VALUES(content), "
                f"updated_at=VALUES(updated_at)",
                (int(account_id), key, content, now))
        conn.commit()
    finally:
        conn.close()
    logging.info(f"Site content '{key}' updated for account {account_id} "
                 f"({len(content)} chars)")
    return {'key': key, 'content': content, 'updated_at': now}
