"""Read-only browser for both databases behind the service.

Two very different stores sit behind this service — the shared MySQL database on
GoDaddy and the local SQLite Rotowire feed — and answering "what is actually in
there?" otherwise means dropping to a mysql client. This exposes both through one
interface.

STRICTLY READ-ONLY. Table and column names are validated against the live catalog
before being interpolated, and no caller-supplied string ever reaches SQL — the only
way to reach a table here is to name one the server already told us exists.
"""

import logging
import os
import sqlite3

import config
import db

MYSQL = 'mysql'
ROTOWIRE = 'rotowire'
PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
def _rotowire_conn():
    if not os.path.exists(config.ROTOWIRE_DB_PATH):
        raise FileNotFoundError(f"No Rotowire database at {config.ROTOWIRE_DB_PATH}")
    return sqlite3.connect(f"file:{config.ROTOWIRE_DB_PATH}?mode=ro", uri=True)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def list_tables(source: str = MYSQL) -> dict:
    """{'ok', 'label', 'tables': [{name, rows, columns}], 'error'}"""
    if source == ROTOWIRE:
        return _list_sqlite()
    return _list_mysql()


def _list_mysql() -> dict:
    out = {'ok': False, 'source': MYSQL, 'label': f'MySQL {config.DB_NAME} @ {config.DB_HOST}',
           'tables': [], 'error': None}
    try:
        conn = db.get_db_connection()
    except Exception as e:
        out['error'] = str(e)[:200]
        return out
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
            )
            names = [r[0] for r in cur.fetchall() or []]
            for name in names:
                cur.execute(
                    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (name,)
                )
                col_row = cur.fetchone()
                rows = None
                try:
                    cur.execute(f"SELECT COUNT(*) FROM `{name}`")
                    count_row = cur.fetchone()
                    rows = int(count_row[0]) if count_row else None
                except Exception:
                    rows = None
                out['tables'].append({'name': name, 'rows': rows,
                                      'columns': int(col_row[0]) if col_row else 0})
        out['ok'] = True
    except Exception as e:
        out['error'] = str(e)[:200]
    finally:
        conn.close()
    return out


def _list_sqlite() -> dict:
    out = {'ok': False, 'source': ROTOWIRE,
           'label': f'SQLite {config.ROTOWIRE_DB_PATH}', 'tables': [], 'error': None}
    try:
        conn = _rotowire_conn()
    except Exception as e:
        out['error'] = str(e)[:200]
        return out
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")
        for (name,) in cur.fetchall() or []:
            cols = cur.execute(f'PRAGMA table_info("{name}")').fetchall()
            try:
                rows = cur.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error:
                rows = None
            out['tables'].append({'name': name, 'rows': rows, 'columns': len(cols)})
        out['ok'] = True
    except Exception as e:
        out['error'] = str(e)[:200]
    finally:
        conn.close()
    return out


def _known_tables(source: str) -> set:
    return {t['name'] for t in list_tables(source).get('tables', [])}


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------
# Columns whose contents must never be shown. The point of this browser is to see the
# data, but an API key or password is exactly the thing that should not be readable
# from a shared admin screen.
REDACTED_COLUMNS = {'key', 'api_key', 'api_key_hash', 'password', 'secret', 'token'}


def _redact(column: str, value):
    if (column or '').strip().lower() in REDACTED_COLUMNS:
        if value in (None, ''):
            return value
        return f'<redacted, {len(str(value))} chars>'
    return value


def read_table(source: str, table: str, offset: int = 0, limit: int = PAGE_SIZE) -> dict:
    """One page of rows. The table name is validated against the live catalog first."""
    out = {'ok': False, 'source': source, 'table': table, 'columns': [], 'rows': [],
           'total': None, 'offset': max(0, int(offset)), 'limit': int(limit), 'error': None}

    if table not in _known_tables(source):
        out['error'] = f"No table named '{table}' in this database."
        return out

    try:
        if source == ROTOWIRE:
            _read_sqlite(out, table)
        else:
            _read_mysql(out, table)
        out['ok'] = True
    except Exception as e:
        logging.warning(f"Browse {source}.{table} failed: {e}")
        out['error'] = str(e)[:200]
    return out


def _read_mysql(out: dict, table: str) -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (table,),
            )
            out['columns'] = [r[0] for r in cur.fetchall() or []]

            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            row = cur.fetchone()
            out['total'] = int(row[0]) if row else 0

            # Table name came from INFORMATION_SCHEMA; offset/limit are ints. LIMIT and
            # OFFSET cannot be parameterised portably in MySQL, hence the int() casts.
            cur.execute(f"SELECT * FROM `{table}` LIMIT {int(out['limit'])} "
                        f"OFFSET {int(out['offset'])}")
            for record in cur.fetchall() or []:
                out['rows'].append([
                    _redact(out['columns'][i] if i < len(out['columns']) else '', v)
                    for i, v in enumerate(record)
                ])
    finally:
        conn.close()


def _read_sqlite(out: dict, table: str) -> None:
    conn = _rotowire_conn()
    try:
        cur = conn.cursor()
        out['columns'] = [c[1] for c in cur.execute(f'PRAGMA table_info("{table}")').fetchall()]
        out['total'] = cur.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        # Newest first for the Rotowire feed — the recent rows are the interesting ones.
        order = ' ORDER BY id DESC' if 'id' in out['columns'] else ''
        cur.execute(f'SELECT * FROM "{table}"{order} LIMIT ? OFFSET ?',
                    (int(out['limit']), int(out['offset'])))
        for record in cur.fetchall() or []:
            out['rows'].append([
                _redact(out['columns'][i] if i < len(out['columns']) else '', v)
                for i, v in enumerate(record)
            ])
    finally:
        conn.close()


def describe_row(source: str, table: str, offset: int) -> dict:
    """A single row expanded to column/value pairs, for wide tables."""
    page = read_table(source, table, offset=offset, limit=1)
    if not page['ok'] or not page['rows']:
        return {'ok': False, 'error': page.get('error') or 'No row at that position',
                'pairs': []}
    return {
        'ok': True,
        'table': table,
        'offset': offset,
        'total': page['total'],
        'pairs': list(zip(page['columns'], page['rows'][0])),
    }
