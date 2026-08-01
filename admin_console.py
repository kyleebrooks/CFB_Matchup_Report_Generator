"""Screen models for the admin console.

Every screen is a pure function: state in, list of display lines out. The curses layer
in admin_tui.py only paints what these return, so the whole UI can be exercised and
asserted headlessly — no terminal required.

A line is (text, style) where style is one of: normal, title, header, ok, warn, err,
dim, sel.
"""

import os
import shutil
import subprocess

import config

NORMAL, TITLE, HEADER, OK, WARN, ERR, DIM, SEL = (
    'normal', 'title', 'header', 'ok', 'warn', 'err', 'dim', 'sel'
)


def _line(text='', style=NORMAL):
    return (text, style)


def rule(width: int = 78, char: str = '-') -> tuple:
    return _line(char * width, DIM)


# ---------------------------------------------------------------------------
# Service inspection
# ---------------------------------------------------------------------------
def service_status(unit: str = 'afplna') -> dict:
    """systemctl is-active / uptime for the API service. Never raises."""
    if not shutil.which('systemctl'):
        return {'available': False, 'active': None, 'detail': 'systemctl not on PATH'}
    try:
        active = subprocess.run(['systemctl', 'is-active', unit],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        since = subprocess.run(
            ['systemctl', 'show', unit, '-p', 'ActiveEnterTimestamp', '--value'],
            capture_output=True, text=True, timeout=5).stdout.strip()
        return {'available': True, 'active': active, 'detail': since or ''}
    except Exception as e:
        return {'available': False, 'active': None, 'detail': str(e)[:80]}


def restart_service(unit: str = 'afplna') -> tuple[bool, str]:
    """Restart the unit. Requires root or a passwordless sudo rule."""
    cmd = ['systemctl', 'restart', unit]
    if os.geteuid() != 0:
        cmd = ['sudo', '-n'] + cmd
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return True, f"{unit} restarted"
        err = (proc.stderr or proc.stdout).strip()[:160]
        if 'password' in err.lower() or 'sudo' in err.lower():
            err += "  (run the console with sudo, or add a passwordless sudo rule)"
        return False, err or f"exit {proc.returncode}"
    except Exception as e:
        return False, str(e)[:160]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def render_dashboard(state: dict) -> list[tuple]:
    out = [
        _line('DASHBOARD', TITLE),
        rule(),
    ]

    svc = state.get('service') or {}
    if not svc.get('available'):
        out.append(_line(f"  Service        : unknown ({svc.get('detail', 'n/a')})", DIM))
    else:
        active = svc.get('active') or 'unknown'
        style = OK if active == 'active' else ERR
        out.append(_line(f"  Service        : {active}", style))
        if svc.get('detail'):
            out.append(_line(f"  Running since  : {svc['detail']}", DIM))

    dbinfo = state.get('db') or {}
    if dbinfo.get('ok'):
        out.append(_line(f"  Database       : connected to {dbinfo.get('database', '?')}"
                         f" @ {config.DB_HOST}", OK))
    else:
        out.append(_line(f"  Database       : UNREACHABLE — {dbinfo.get('error', '')[:50]}", ERR))
        if (state.get('env') or {}).get('missing'):
            out.append(_line("                   The service environment did not load — run "
                             "'admin_tui.py env'", WARN))

    accs = state.get('accounts')
    if accs is None:
        out.append(_line('  Accounts       : unavailable', ERR))
    else:
        active_n = sum(1 for a in accs if a['active'])
        admin_n = sum(1 for a in accs if a['is_admin'])
        out.append(_line(f"  Accounts       : {len(accs)} total, {active_n} active, "
                         f"{admin_n} admin"))

    schema_report = state.get('schema') or {}
    if schema_report:
        import schema as schema_mod
        text = schema_mod.summary_line(schema_report)
        out.append(_line(f"  Schema         : {text}", OK if schema_report.get('ok') else WARN))

    out.append(_line())
    out.append(_line('  EFFECTIVE REPORT SETTINGS', HEADER))
    for row in state.get('settings') or []:
        marker = '*' if row['overridden'] else ' '
        origin = 'db override' if row['overridden'] else 'env default'
        out.append(_line(f"  {marker} {row['key']:<22} {str(row['value']):<34} ({origin})",
                         WARN if row['overridden'] else NORMAL))

    out.append(_line())
    out.append(_line('  PATHS', HEADER))
    for label, path in (('Reports', config.REPORTS_DIR),
                        ('Watermarks', config.WATERMARKS_DIR),
                        ('Rotowire DB', config.ROTOWIRE_DB_PATH)):
        exists = os.path.exists(path)
        out.append(_line(f"    {label:<12} {path}", NORMAL if exists else WARN))

    env = state.get('env') or {}
    if env.get('source') and env['source'] != 'environment':
        out.append(_line(f"  Environment    : loaded from {env['source']}", DIM))
    elif env.get('missing'):
        out.append(_line(f"  Environment    : MISSING {', '.join(env['missing'])}", ERR))

    out.append(_line())
    out.append(_line(f"  Admin bootstrap key set: "
                     f"{'yes' if config.ADMIN_API_KEY else 'NO — set ADMIN_API_KEY'}",
                     OK if config.ADMIN_API_KEY else WARN))
    return out


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def render_accounts(state: dict) -> list[tuple]:
    accounts_list = state.get('accounts')
    selected = state.get('selected', 0)

    out = [
        _line('ACCOUNTS', TITLE),
        rule(),
    ]
    if accounts_list is None:
        out.append(_line('  Could not load accounts (database unreachable).', ERR))
        return out
    if not accounts_list:
        out.append(_line('  No accounts yet. Press [n] to create one.', DIM))
        return out

    usage_rows = state.get('usage') or {}
    out.append(_line(f"  {'ID':<4} {'NAME':<24} {'KEY':<13} {'ST':<4} {'REPORTS':<16} "
                     f"{'WM':<3} {'CALLS':>6} {'30D':>5}", HEADER))
    for i, a in enumerate(accounts_list):
        flag = 'ON ' if a['active'] else 'off'
        if a['is_admin']:
            flag += '*'
        reports = ','.join(a['allowed_reports'] or []) or '-'
        wm = 'yes' if a.get('watermark_file') else '-'
        stats = usage_rows.get(a['id']) or {}
        text = (f"  {a['id']:<4} {a['account_name'][:23]:<24} {a['api_key_prefix'][:12]:<13} "
                f"{flag:<4} {reports[:15]:<16} {wm:<3} "
                f"{stats.get('total', 0):>6} {stats.get('last_30d', 0):>5}")
        out.append(_line(text, SEL if i == selected else
                         (NORMAL if a['active'] else DIM)))

    out.append(_line())
    out.append(_line('  * = admin account   CALLS = report requests all time   30D = last 30 days',
                     DIM))
    return out


def render_account_detail(account: dict, settings_rows: list[dict],
                          usage_summary: dict | None = None,
                          history: list[dict] | None = None) -> list[tuple]:
    out = [
        _line(f"ACCOUNT {account['id']} — {account['account_name']}", TITLE),
        rule(),
        _line(f"  Key prefix     : {account['api_key_prefix']}..."),
        _line(f"  Contact        : {account.get('contact_email') or '-'}"),
        _line(f"  Active         : {'yes' if account['active'] else 'no'}",
              OK if account['active'] else WARN),
        _line(f"  Admin          : {'yes' if account['is_admin'] else 'no'}",
              WARN if account['is_admin'] else NORMAL),
        _line(f"  Report types   : {', '.join(account['allowed_reports'] or []) or '(none)'}"),
        _line(f"  Watermark      : {account.get('watermark_file') or 'service default'}"),
        _line(f"  Created        : {account['created_at']}", DIM),
        _line(f"  Updated        : {account['updated_at']}", DIM),
        _line(),
        _line('  SETTINGS (this account overrides service defaults)', HEADER),
    ]
    overrides = account.get('settings') or {}
    for row in settings_rows:
        key = row['key']
        if key in overrides:
            out.append(_line(f"  * {key:<22} {str(overrides[key]):<30} (account override)", WARN))
        else:
            out.append(_line(f"    {key:<22} {str(row['value']):<30} (from {row['source']})", DIM))

    stats = usage_summary or {}
    out.append(_line())
    out.append(_line('  API USAGE', HEADER))
    last = stats.get('last_used')
    out.append(_line(f"    Total requests   : {stats.get('total', 0)}"))
    out.append(_line(f"    Completed        : {stats.get('done', 0)}", OK))
    out.append(_line(f"    Failed           : {stats.get('error', 0)}",
                     ERR if stats.get('error') else DIM))
    out.append(_line(f"    In progress      : {stats.get('running', 0)}", DIM))
    out.append(_line(f"    Last 30 days     : {stats.get('last_30d', 0)}"))
    out.append(_line(f"    Last used        : "
                     f"{last.strftime('%Y-%m-%d %H:%M UTC') if hasattr(last, 'strftime') else (last or 'never')}"))
    for rtype, count in sorted((stats.get('by_type') or {}).items()):
        out.append(_line(f"    {rtype:<16} : {count}", DIM))

    if history:
        out.append(_line())
        out.append(_line('  RECENT REQUESTS', HEADER))
        out.append(_line(f"    {'WHEN':<18} {'TYPE':<9} {'SUBJECT':<26} {'STATE':<8} SECS", DIM))
        for h in history[:12]:
            when = h['created_at']
            when = when.strftime('%Y-%m-%d %H:%M') if hasattr(when, 'strftime') else str(when)[:16]
            style = OK if h['state'] == 'done' else (ERR if h['state'] == 'error' else DIM)
            out.append(_line(f"    {when:<18} {(h['report_type'] or '-')[:8]:<9} "
                             f"{(h['subject'] or '-')[:25]:<26} {(h['state'] or '-')[:7]:<8} "
                             f"{h['seconds'] or '-'}", style))
            if h['state'] == 'error' and h.get('error'):
                out.append(_line(f"      {str(h['error'])[:70]}", ERR))
    return out


# ---------------------------------------------------------------------------
# Database browser
# ---------------------------------------------------------------------------
def render_browser(state: dict) -> list[tuple]:
    listing = state.get('browse_tables') or {}
    page = state.get('browse_page')
    selected = state.get('selected', 0)

    out = [
        _line(f"DATABASE BROWSER — {listing.get('label', '?')}", TITLE),
        rule(),
    ]
    if listing.get('error'):
        out.append(_line(f"  {listing['error']}", ERR))
        return out

    if page:
        return out + _render_browse_page(page)

    out.append(_line('  [b] switch database   ENTER open table   read-only', DIM))
    out.append(_line())
    out.append(_line(f"  {'TABLE':<34} {'ROWS':>10}  COLS", HEADER))
    for i, t in enumerate(listing.get('tables') or []):
        rows = '?' if t['rows'] is None else f"{t['rows']:,}"
        out.append(_line(f"  {t['name'][:33]:<34} {rows:>10}  {t['columns']}",
                         SEL if i == selected else NORMAL))
    if not listing.get('tables'):
        out.append(_line('  (no tables)', DIM))
    return out


def _render_browse_page(page: dict) -> list[tuple]:
    out = []
    if page.get('error'):
        return [_line(f"  {page['error']}", ERR)]

    total = page.get('total') or 0
    start = page['offset'] + 1 if total else 0
    end = min(page['offset'] + page['limit'], total)
    out.append(_line(f"  Table: {page['table']}    rows {start}-{end} of {total:,}", HEADER))
    out.append(_line('  [n] next page   [p] previous   ESC back to the table list', DIM))
    out.append(_line())

    columns = page.get('columns') or []
    # Wide tables are unreadable side by side, so switch to one record per block.
    if len(columns) > 6:
        for idx, row in enumerate(page.get('rows') or []):
            out.append(_line(f"  --- row {page['offset'] + idx + 1} ---", HEADER))
            for col, value in zip(columns, row):
                text = '' if value is None else str(value).replace('\n', ' ')
                out.append(_line(f"    {col[:22]:<24} {text[:96]}"))
            out.append(_line())
    else:
        widths = [max(10, min(28, len(c) + 2)) for c in columns]
        head = '  ' + ''.join(c[:w - 1].ljust(w) for c, w in zip(columns, widths))
        out.append(_line(head, HEADER))
        for row in page.get('rows') or []:
            cells = []
            for value, w in zip(row, widths):
                text = '' if value is None else str(value).replace('\n', ' ')
                cells.append(text[:w - 1].ljust(w))
            out.append(_line('  ' + ''.join(cells)))
    if not page.get('rows'):
        out.append(_line('  (no rows on this page)', DIM))
    return out


# ---------------------------------------------------------------------------
# Report catalog
# ---------------------------------------------------------------------------
def render_catalog(state: dict) -> list[tuple]:
    reports = state.get('catalog') or []
    selected = state.get('selected', 0)

    out = [
        _line('REPORT CATALOG', TITLE),
        rule(),
        _line('  up/down select   ENTER show every section and how it is produced', DIM),
        _line(),
    ]
    for i, r in enumerate(reports):
        out.append(_line(f"  {r['report_type']:<12} {r['title'][:44]:<46} "
                         f"{len(r['sections'])} sections, {len(r['charts'])} charts",
                         SEL if i == selected else NORMAL))
    out.append(_line())
    import catalog as catalog_mod
    for note in catalog_mod.PIPELINE_NOTES:
        out.append(_line(f"  {note}", DIM))
    return out


def render_catalog_detail(report: dict) -> list[tuple]:
    out = [
        _line(f"{report['title'].upper()}  ({report['report_type']})", TITLE),
        rule(),
        _line(f"  {report['description']}", DIM),
        _line(),
        _line(f"  Required params : {', '.join(report['required_params'])}"),
        _line(f"  Optional params : {', '.join(report['optional_params']) or '-'}"),
        _line(f"  Research calls  : {report['research_calls']} "
              f"(parallel, one per news section)"),
        _line(f"  Research model  : {report['research_model']}"),
        _line(f"  Report model    : {report['report_model']}"),
        _line(f"  Search engine   : {report['search_engine']} "
              f"({report['search_max_results']} results per call)"),
        _line(),
        _line('  DATA SOURCES', HEADER),
    ]
    for src in report['data_sources']:
        out.append(_line(f"    - {src}"))

    out.append(_line())
    out.append(_line(f"  SECTIONS, IN ORDER ({len(report['sections'])})", HEADER))
    for i, sec in enumerate(report['sections'], start=1):
        out.append(_line(f"    {i:>2}. {sec['title']}", NORMAL))
        window = f" (last {sec['window_days']} days)" if sec.get('window_days') else ''
        out.append(_line(f"        source: {sec['source']}{window}", DIM))

    out.append(_line())
    out.append(_line(f"  CHARTS ({len(report['charts'])})", HEADER))
    for c in report['charts']:
        out.append(_line(f"    - {c['title']}"))
        out.append(_line(f"        {c['caption'][:88]}", DIM))
    return out


# ---------------------------------------------------------------------------
# API examples
# ---------------------------------------------------------------------------
def render_examples(state: dict) -> list[tuple]:
    blocks = state.get('examples') or []
    target = state.get('examples_for') or 'all accounts'

    out = [
        _line(f"API EXAMPLES — {target}", TITLE),
        rule(),
        _line('  [a] pick an account   [d] administrator calls   [w] write to a file', DIM),
        _line('  Copy any block straight into a terminal. Keys are never included.', DIM),
        _line(),
    ]
    for block in blocks:
        out.append(_line(f"  {block['title']}", HEADER))
        if block.get('note'):
            out.append(_line(f"    {block['note'][:92]}", DIM))
        for line in block['lines']:
            out.append(_line(f"    {line}"))
        out.append(_line())
    return out


# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------
def render_global_settings(state: dict) -> list[tuple]:
    rows = state.get('settings') or []
    selected = state.get('selected', 0)

    out = [
        _line('SERVICE-WIDE SETTINGS', TITLE),
        rule(),
        _line('  These apply to every account that has not overridden them.', DIM),
        _line('  Changes are live — no service restart needed.', DIM),
        _line(),
        _line(f"  {'SETTING':<24} {'VALUE':<32} SOURCE", HEADER),
    ]
    for i, row in enumerate(rows):
        marker = '*' if row['overridden'] else ' '
        origin = 'database' if row['overridden'] else 'environment'
        text = f"  {marker}{row['key']:<23} {str(row['value'])[:31]:<32} {origin}"
        out.append(_line(text, SEL if i == selected else
                         (WARN if row['overridden'] else NORMAL)))

    out.append(_line())
    out.append(_line('  ENVIRONMENT-ONLY (edit /etc/afplna.env, then restart)', HEADER))
    import settings_store
    for name in settings_store.ENV_ONLY:
        value = getattr(config, name, None)
        if name in ('SERVICE_API_KEY', 'ADMIN_API_KEY'):
            shown = f"set ({len(str(value))} chars)" if value else 'NOT SET'
        else:
            shown = str(value)
        out.append(_line(f"    {name:<22} {shown}", DIM))
    return out


# ---------------------------------------------------------------------------
# Schema audit
# ---------------------------------------------------------------------------
def render_schema(report: dict) -> list[tuple]:
    out = [
        _line('DATABASE AUDIT', TITLE),
        rule(),
    ]
    if report.get('error'):
        out.append(_line(f"  {report['error']}", ERR))
        return out

    out.append(_line(f"  Database: {report.get('database')} @ {config.DB_HOST}"))
    out.append(_line())
    out.append(_line('  TABLES', HEADER))
    for t in report.get('tables', []):
        if t['status'] == 'ok':
            style, note = OK, f"ok ({t['rows']} rows)" if t['rows'] is not None else 'ok'
        elif t['status'] == 'missing':
            style, note = WARN, 'MISSING — will be created'
        elif t['status'] == 'missing-external':
            style, note = ERR, 'MISSING — pre-existing table, not created by us'
        else:
            bits = []
            if t['missing_columns']:
                bits.append(f"missing columns: {', '.join(t['missing_columns'])}")
            if t['missing_indexes']:
                bits.append(f"missing indexes: {', '.join(t['missing_indexes'])}")
            style, note = WARN, '; '.join(bits)
        out.append(_line(f"    {t['table']:<26} {note}", style))
        out.append(_line(f"      {t['purpose']}", DIM))
        if t.get('extra_columns'):
            out.append(_line(f"      extra columns (left alone): "
                             f"{', '.join(t['extra_columns'])}", DIM))

    if report.get('api_keys'):
        out.append(_line())
        out.append(_line('  API_KEYS ROWS  (presence only — values are never displayed)', HEADER))
        for k in report['api_keys']:
            if k['has_value']:
                style, note = OK, 'present'
            elif k['present']:
                style, note = WARN, 'row exists but the value is empty'
            else:
                style, note = (ERR if k['required'] else DIM), 'missing'
            req = 'required' if k['required'] else 'optional'
            out.append(_line(f"    {k['name']:<20} {note:<32} ({req})", style))
            out.append(_line(f"      {k['purpose']}", DIM))

    fixes = report.get('fixes') or []
    out.append(_line())
    if fixes:
        out.append(_line(f"  PROPOSED REPAIRS ({len(fixes)}) — additive only, nothing is dropped",
                         HEADER))
        for f in fixes:
            out.append(_line(f"    + {f['describe']}", WARN))
        out.append(_line())
        out.append(_line('  Press [a] to apply these repairs.', WARN))
    else:
        out.append(_line('  Nothing to repair.', OK))
    return out


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def render_health(report: dict) -> list[tuple]:
    out = [
        _line('HEALTH CHECK', TITLE),
        rule(),
    ]
    if not report:
        out.append(_line('  Press [r] to run the checks (makes live API calls).', DIM))
        return out
    if report.get('_error'):
        out.append(_line(f"  {report['_error']}", ERR))
        return out

    overall = report.get('ok')
    out.append(_line(f"  Overall: {'OK' if overall else 'PROBLEMS FOUND'}", OK if overall else ERR))
    out.append(_line(f"  Season year in use: {report.get('season_year')}", DIM))
    out.append(_line())

    for name, check in (report.get('checks') or {}).items():
        ok = check.get('ok')
        out.append(_line(f"  {name:<16} {'ok' if ok else 'FAILED'}", OK if ok else ERR))
        for key, value in check.items():
            if key == 'ok':
                continue
            if key == 'models' and isinstance(value, dict):
                for role, m in value.items():
                    mok = m.get('ok')
                    out.append(_line(f"      {role:<10} {m.get('model', '')} "
                                     f"{'ok' if mok else 'FAILED'}", OK if mok else ERR))
                    if m.get('error'):
                        out.append(_line(f"        {str(m['error'])[:70]}", ERR))
                continue
            out.append(_line(f"      {key}: {str(value)[:66]}", DIM))
    return out


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
HELP = [
    _line('AFPLNA REPORT SERVICE — ADMIN CONSOLE', TITLE),
    rule(),
    _line(),
    _line('  GLOBAL KEYS', HEADER),
    _line('    1..8     switch screen        q      quit'),
    _line('    r        refresh this screen  ?      this help'),
    _line('    ESC      back / close'),
    _line(),
    _line('  ACCOUNTS SCREEN', HEADER),
    _line('    up/down  select               ENTER  account detail'),
    _line('    n        new account          k      rotate API key'),
    _line('    e        edit report types    s      edit a setting'),
    _line('    t        toggle active        m      toggle admin'),
    _line('    w        clear watermark      D      DELETE account (permanent)'),
    _line('    Account detail shows call counts and recent request history.'),
    _line(),
    _line('  SETTINGS SCREEN', HEADER),
    _line('    up/down  select               ENTER  change value'),
    _line('    x        clear override (revert to the environment default)'),
    _line(),
    _line('  DATABASE SCREEN', HEADER),
    _line('    a        apply proposed repairs (additive: CREATE TABLE / ADD COLUMN)'),
    _line(),
    _line('  BROWSER SCREEN (6)', HEADER),
    _line('    b        switch MySQL <-> Rotowire SQLite'),
    _line('    ENTER    open table    n/p  next/previous page    ESC  back'),
    _line('    Read-only. Key and password columns are redacted.'),
    _line(),
    _line('  REPORTS SCREEN (7)', HEADER),
    _line('    ENTER    every section of that report and how each one is produced'),
    _line(),
    _line('  EXAMPLES SCREEN (8)', HEADER),
    _line('    a        build examples for one account   d  administrator calls'),
    _line('    w        write the examples to a file'),
    _line(),
    _line('  NOTES', HEADER),
    _line('    API keys are stored as hashes. A new key is shown ONCE, at creation'),
    _line('    or rotation, and cannot be recovered afterwards — only rotated again.'),
    _line(),
    _line('    In any input, a suggested value is replaced by the first key you type;'),
    _line('    ^U clears the line, ESC cancels.'),
    _line(),
    _line('    Service-wide and per-account settings take effect immediately.'),
    _line('    Environment-only settings need an /etc/afplna.env edit and a restart.'),
]
