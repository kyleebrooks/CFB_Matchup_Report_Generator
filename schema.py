"""Declarative database schema: audit what exists, report gaps, repair additively.

The service owns two tables in the shared kdogg4207 database and depends on a third
(API_KEYS) that predates it. This module can inspect all three and create or extend
what is missing.

Repairs are ADDITIVE ONLY — CREATE TABLE and ADD COLUMN/ADD INDEX. Nothing here ever
drops, renames, narrows or rewrites anything, so running it against a database with
existing data is safe.
"""

import logging

import db

# ---------------------------------------------------------------------------
# Declarative spec
# ---------------------------------------------------------------------------
# Each column maps to the DDL fragment used by ALTER TABLE ... ADD COLUMN.
SPEC: dict[str, dict] = {
    'report_accounts': {
        'owner': 'service',
        'purpose': 'Multi-tenant API accounts, entitlements and per-account settings',
        'create': """
            CREATE TABLE IF NOT EXISTS report_accounts (
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
        """,
        'columns': {
            'id':              'INT AUTO_INCREMENT PRIMARY KEY',
            'account_name':    'VARCHAR(120) NOT NULL',
            'contact_email':   'VARCHAR(190) DEFAULT NULL',
            'api_key_hash':    'CHAR(64) NOT NULL',
            'api_key_prefix':  'VARCHAR(24) NOT NULL',
            'is_admin':        'TINYINT(1) NOT NULL DEFAULT 0',
            'active':          'TINYINT(1) NOT NULL DEFAULT 1',
            'allowed_reports': 'TEXT DEFAULT NULL',
            'settings':        'TEXT DEFAULT NULL',
            'watermark_file':  'VARCHAR(255) DEFAULT NULL',
            'created_at':      'DATETIME NOT NULL',
            'updated_at':      'DATETIME NOT NULL',
        },
        'indexes': {
            'uniq_api_key_hash': 'ADD UNIQUE KEY uniq_api_key_hash (api_key_hash)',
        },
    },
    'report_service_settings': {
        'owner': 'service',
        'purpose': 'Service-wide setting overrides (models, search depth) — live, no restart',
        'create': """
            CREATE TABLE IF NOT EXISTS report_service_settings (
                setting_key   VARCHAR(64)  NOT NULL PRIMARY KEY,
                setting_value TEXT         DEFAULT NULL,
                updated_at    DATETIME     NOT NULL,
                updated_by    VARCHAR(120) DEFAULT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        'columns': {
            'setting_key':   'VARCHAR(64) NOT NULL',
            'setting_value': 'TEXT DEFAULT NULL',
            'updated_at':    'DATETIME NOT NULL',
            'updated_by':    'VARCHAR(120) DEFAULT NULL',
        },
        'indexes': {},
    },
    'report_usage': {
        'owner': 'service',
        'purpose': 'One row per API report request — per-account call tracking',
        'create': """
            CREATE TABLE IF NOT EXISTS report_usage (
                id           INT AUTO_INCREMENT PRIMARY KEY,
                account_id   INT          NOT NULL,
                report_type  VARCHAR(40)  DEFAULT NULL,
                subject      VARCHAR(190) DEFAULT NULL,
                job_id       VARCHAR(32)  DEFAULT NULL,
                state        VARCHAR(16)  DEFAULT NULL,
                seconds      INT          DEFAULT NULL,
                sources      INT          DEFAULT NULL,
                error        VARCHAR(255) DEFAULT NULL,
                created_at   DATETIME     NOT NULL,
                completed_at DATETIME     DEFAULT NULL,
                KEY idx_usage_account (account_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        'columns': {
            'id':           'INT AUTO_INCREMENT PRIMARY KEY',
            'account_id':   'INT NOT NULL',
            'report_type':  'VARCHAR(40) DEFAULT NULL',
            'subject':      'VARCHAR(190) DEFAULT NULL',
            'job_id':       'VARCHAR(32) DEFAULT NULL',
            'state':        'VARCHAR(16) DEFAULT NULL',
            'seconds':      'INT DEFAULT NULL',
            'sources':      'INT DEFAULT NULL',
            'error':        'VARCHAR(255) DEFAULT NULL',
            'created_at':   'DATETIME NOT NULL',
            'completed_at': 'DATETIME DEFAULT NULL',
        },
        'indexes': {
            'idx_usage_account': 'ADD KEY idx_usage_account (account_id, created_at)',
        },
    },
    'API_KEYS': {
        'owner': 'pre-existing',
        'purpose': 'Shared provider key store (predates this service — audited, never altered)',
        'create': None,          # never created or modified by us
        'columns': {'API_NAME': None, 'KEY': None},
        'indexes': {},
    },
}

# Key rows the service looks for in API_KEYS. Presence only — values are never shown.
REQUIRED_KEY_ROWS = [
    ('openrouter', True,  'All LLM calls (research + report synthesis)'),
    ('CFD',        True,  'CollegeFootballData statistics'),
    ('bright',     False, 'Bright Data collector for the Rotowire scrape'),
    ('cfbmatchupreport', False, 'Service key the afplnapicks.com proxy presents'),
]


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------
def _existing_columns(cur, table: str) -> dict:
    cur.execute(
        "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
        "FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return {r[0]: {'type': r[1], 'nullable': r[2], 'default': r[3]} for r in cur.fetchall() or []}


def _existing_indexes(cur, table: str) -> set:
    cur.execute(
        "SELECT DISTINCT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return {r[0] for r in cur.fetchall() or []}


def _row_count(cur, table: str) -> int | None:
    try:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return None


def audit() -> dict:
    """Inspect the database and report what exists, what is missing, and the fixes.

    Read-only. Returns a structure the TUI renders and `apply()` consumes.
    """
    result = {'ok': True, 'database': None, 'tables': [], 'api_keys': [], 'fixes': [], 'error': None}

    try:
        conn = db.get_db_connection()
    except Exception as e:
        result['ok'] = False
        result['error'] = f"Cannot connect to the database: {e}"
        return result

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE()")
            row = cur.fetchone()
            result['database'] = row[0] if row else '(unknown)'

            for table, spec in SPEC.items():
                columns = _existing_columns(cur, table)
                exists = bool(columns)
                entry = {
                    'table': table,
                    'owner': spec['owner'],
                    'purpose': spec['purpose'],
                    'exists': exists,
                    'rows': _row_count(cur, table) if exists else None,
                    'missing_columns': [],
                    'missing_indexes': [],
                    'extra_columns': [],
                    'status': 'ok',
                }

                if not exists:
                    if spec['create']:
                        entry['status'] = 'missing'
                        result['ok'] = False
                        result['fixes'].append({
                            'table': table,
                            'kind': 'create_table',
                            'sql': ' '.join(spec['create'].split()),
                            'describe': f"CREATE TABLE {table}",
                        })
                    else:
                        entry['status'] = 'missing-external'
                        result['ok'] = False
                    result['tables'].append(entry)
                    continue

                wanted = spec['columns']
                entry['missing_columns'] = [c for c in wanted if c not in columns]
                entry['extra_columns'] = [c for c in columns if c not in wanted]

                indexes = _existing_indexes(cur, table)
                entry['missing_indexes'] = [i for i in spec['indexes'] if i not in indexes]

                if entry['missing_columns'] or entry['missing_indexes']:
                    entry['status'] = 'incomplete'
                    result['ok'] = False

                # Only propose repairs for tables we own.
                if spec['create']:
                    for col in entry['missing_columns']:
                        ddl = wanted[col]
                        if ddl and 'AUTO_INCREMENT' not in ddl:
                            result['fixes'].append({
                                'table': table,
                                'kind': 'add_column',
                                'sql': f"ALTER TABLE `{table}` ADD COLUMN `{col}` {ddl}",
                                'describe': f"ADD COLUMN {table}.{col}",
                            })
                    for idx in entry['missing_indexes']:
                        result['fixes'].append({
                            'table': table,
                            'kind': 'add_index',
                            'sql': f"ALTER TABLE `{table}` {spec['indexes'][idx]}",
                            'describe': f"ADD INDEX {table}.{idx}",
                        })

                result['tables'].append(entry)

            # API_KEYS rows — presence only, never values.
            api_keys_present = any(t['table'] == 'API_KEYS' and t['exists'] for t in result['tables'])
            if api_keys_present:
                for name, required, purpose in REQUIRED_KEY_ROWS:
                    found, has_value = False, False
                    try:
                        cur.execute(
                            "SELECT `KEY` FROM API_KEYS WHERE API_NAME=%s LIMIT 1", (name,)
                        )
                        row = cur.fetchone()
                        if row:
                            found = True
                            has_value = bool(row[0] and str(row[0]).strip())
                    except Exception as e:
                        logging.warning(f"API_KEYS probe for {name} failed: {e}")
                    result['api_keys'].append({
                        'name': name, 'required': required, 'purpose': purpose,
                        'present': found, 'has_value': has_value,
                    })
                    if required and not (found and has_value):
                        result['ok'] = False
    finally:
        conn.close()

    return result


def apply(fixes: list[dict]) -> list[dict]:
    """Run the proposed repairs. Returns one result row per fix."""
    if not fixes:
        return []
    out = []
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            for fix in fixes:
                try:
                    cur.execute(fix['sql'])
                    out.append({**fix, 'ok': True, 'error': None})
                    logging.info(f"Schema repair applied: {fix['describe']}")
                except Exception as e:
                    out.append({**fix, 'ok': False, 'error': str(e)[:300]})
                    logging.error(f"Schema repair FAILED ({fix['describe']}): {e}")
    finally:
        conn.close()
    return out


def summary_line(report: dict) -> str:
    if report.get('error'):
        return f"DB unreachable — {report['error'][:60]}"
    missing = [t['table'] for t in report['tables'] if t['status'] != 'ok']
    bad_keys = [k['name'] for k in report['api_keys'] if k['required'] and not k['has_value']]
    if not missing and not bad_keys:
        return f"Schema OK ({len(report['tables'])} tables checked)"
    bits = []
    if missing:
        bits.append(f"{len(missing)} table(s) need attention: {', '.join(missing)}")
    if bad_keys:
        bits.append(f"missing key row(s): {', '.join(bad_keys)}")
    return "; ".join(bits)
