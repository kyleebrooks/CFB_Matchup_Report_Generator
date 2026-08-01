#!/usr/bin/env python3
"""Terminal admin console for the AFPLNA report service.

Run it over SSH:

    cd /opt/afplna && ./venv/bin/python admin_tui.py

Everything it can do interactively is also available non-interactively, which matters
when the terminal cannot do curses (a bare TERM, a cron job, a pipe):

    admin_tui.py audit [--apply]      database audit, optionally repair
    admin_tui.py accounts             list accounts
    admin_tui.py create NAME [types]  create an account, print the key once
    admin_tui.py rotate ID            issue a new key
    admin_tui.py settings             show effective settings
    admin_tui.py set KEY VALUE        set a service-wide override
    admin_tui.py unset KEY            clear a service-wide override
    admin_tui.py env                  show where the service environment came from
    admin_tui.py delete ID            permanently delete an account
    admin_tui.py usage [ID]           API call counts, overall or for one account
    admin_tui.py tables [rotowire]    list tables in a database
    admin_tui.py show TABLE [OFFSET]  page through a table's rows (read-only)
    admin_tui.py reports [TYPE]       report catalog: sections and how they are made
    admin_tui.py examples [ID|admin]  copy-pasteable API calls

This is a thin view layer: the screens come from admin_console.py as plain text, and
every mutation goes through accounts.py / settings_store.py / schema.py — the same code
paths the REST API uses.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The service reads /etc/afplna.env through systemd; an interactive shell does not.
# Fill the gap BEFORE importing config, which snapshots os.environ at import time.
# Without this the console fails with a database "Access denied ... (using password: NO)",
# which points at the database when the real problem is a missing environment.
import envfile            # noqa: E402
ENV_REPORT = envfile.bootstrap()

import accounts            # noqa: E402
import admin_console as ui  # noqa: E402
import catalog             # noqa: E402
import config              # noqa: E402
import dbbrowse            # noqa: E402
import examples            # noqa: E402
import report_types        # noqa: E402
import schema              # noqa: E402
import settings_store      # noqa: E402
import usage               # noqa: E402

SCREENS = ['Dashboard', 'Accounts', 'Settings', 'Database', 'Health',
           'Browser', 'Reports', 'Examples']


# ---------------------------------------------------------------------------
# State loading (shared by the TUI and the CLI)
# ---------------------------------------------------------------------------
def load_state(screen: str = 'Dashboard') -> dict:
    """Gather everything the requested screen needs. Never raises."""
    state = {'screen': screen, 'errors': []}

    try:
        report = schema.audit()
        state['schema'] = report
        state['db'] = {'ok': not report.get('error'),
                       'database': report.get('database'),
                       'error': report.get('error') or ''}
    except Exception as e:
        state['schema'] = {'error': str(e)}
        state['db'] = {'ok': False, 'error': str(e)}

    try:
        state['accounts'] = accounts.list_all()
    except Exception as e:
        state['accounts'] = None
        state['errors'].append(f"accounts: {e}")

    try:
        state['settings'] = settings_store.describe()
    except Exception as e:
        state['settings'] = []
        state['errors'].append(f"settings: {e}")

    try:
        state['usage'] = usage.summary_by_account()
    except Exception as e:
        state['usage'] = {}
        state['errors'].append(f"usage: {e}")

    state['service'] = ui.service_status()
    state['env'] = ENV_REPORT
    return state


def env_ok() -> bool:
    return not ENV_REPORT.get('missing')


def print_env_guidance(stream=sys.stderr) -> None:
    for line in envfile.guidance(ENV_REPORT):
        print(line, file=stream)


def preflight() -> str | None:
    """Confirm the database is actually reachable before doing anything else.

    A missing DB_PASSWORD is only a *hint* that the environment was not loaded — a
    database could be reachable some other way. So probe first and refuse only when
    the connection genuinely fails, rather than blocking a working setup on a proxy
    signal. Returns the database error when the environment is to blame, else None.
    """
    probe = schema.audit()
    if not probe.get('error'):
        return None
    if env_ok():
        return None          # reachable-looking env; the caller reports the real error
    return probe['error']


def run_health() -> dict:
    """Call the service's own /health in-process, so no key or network hop is needed."""
    try:
        os.environ.setdefault('SKIP_SCHEDULER', '1')
        import app as app_mod
        client = app_mod.app.test_client()
        saved, config.SERVICE_API_KEY = config.SERVICE_API_KEY, None
        try:
            return client.get('/health').get_json() or {'_error': 'empty response'}
        finally:
            config.SERVICE_API_KEY = saved
    except Exception as e:
        return {'_error': f"{e.__class__.__name__}: {e}"}


# ---------------------------------------------------------------------------
# curses front end
# ---------------------------------------------------------------------------
def _curses_main(stdscr):
    import curses

    curses.curs_set(0)
    stdscr.keypad(True)
    have_color = curses.has_colors()
    if have_color:
        curses.start_color()
        curses.use_default_colors()
        for idx, fg in ((1, curses.COLOR_CYAN), (2, curses.COLOR_GREEN),
                        (3, curses.COLOR_YELLOW), (4, curses.COLOR_RED),
                        (5, curses.COLOR_BLUE), (6, curses.COLOR_WHITE)):
            curses.init_pair(idx, fg, -1)
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_CYAN)

    STYLES = {
        ui.NORMAL: 0,
        ui.TITLE:  (curses.color_pair(1) | curses.A_BOLD) if have_color else curses.A_BOLD,
        ui.HEADER: (curses.color_pair(5) | curses.A_BOLD) if have_color else curses.A_BOLD,
        ui.OK:     curses.color_pair(2) if have_color else 0,
        ui.WARN:   curses.color_pair(3) if have_color else 0,
        ui.ERR:    (curses.color_pair(4) | curses.A_BOLD) if have_color else curses.A_BOLD,
        ui.DIM:    curses.A_DIM,
        ui.SEL:    curses.color_pair(7) if have_color else curses.A_REVERSE,
    }

    state = load_state()
    screen = 'Dashboard'
    selected = 0
    scroll = 0
    flash = ('', ui.NORMAL)
    detail_of = None
    health = {}
    browse_source = dbbrowse.MYSQL
    browse_tables = None
    browse_page = None
    catalog_detail = None
    examples_blocks = None
    examples_for = None

    # -- primitives -------------------------------------------------------
    def paint(lines):
        nonlocal scroll
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        body_h = height - 5

        tabs = '  '.join(f"[{i + 1}]{n}" for i, n in enumerate(SCREENS))
        stdscr.addnstr(0, 0, f" AFPLNA Admin Console   {tabs}", width - 1,
                       STYLES[ui.HEADER])
        stdscr.addnstr(1, 0, '=' * (width - 1), width - 1, STYLES[ui.DIM])

        scroll = max(0, min(scroll, max(0, len(lines) - body_h)))
        for row, (text, style) in enumerate(lines[scroll:scroll + body_h]):
            try:
                stdscr.addnstr(2 + row, 0, text, width - 1, STYLES.get(style, 0))
            except Exception:
                pass

        msg, msg_style = flash
        # Two footer rows: what THIS screen can do, then the global keys. The
        # per-screen bar lives here rather than in the body so a long list can never
        # scroll it out of sight.
        keys = ui.KEY_BAR.get(screen, '')
        footer = msg or "  [1-8] screen   [r] refresh   [?] help   [q] quit"
        try:
            stdscr.addnstr(height - 3, 0, '=' * (width - 1), width - 1, STYLES[ui.DIM])
            stdscr.addnstr(height - 2, 0, f"  {keys}".ljust(width - 1), width - 1,
                           STYLES[ui.WARN])
            stdscr.addnstr(height - 1, 0, footer.ljust(width - 1), width - 1,
                           STYLES.get(msg_style, 0))
        except Exception:
            pass
        stdscr.refresh()

    def prompt(label, default='', allow_empty=False):
        """Single-line input at the footer. ESC cancels and returns None.

        A pre-filled default used to be a trap: typing appended to it, so entering
        "team" over a "team,matchup" default produced "team,matchupteam" and the
        action failed validation. Now the first printable keystroke replaces the
        suggestion outright, and Ctrl-U clears the line at any point.
        """
        curses.curs_set(1)
        buf = list(str(default))
        pristine = bool(buf)          # default still untouched?
        height, width = stdscr.getmaxyx()
        try:
            while True:
                shown = ''.join(buf)
                hint = '  (typing replaces; ^U clears; ESC cancels)' if pristine else ''
                text = f" {label}: {shown}{hint}"
                stdscr.addnstr(height - 1, 0, text.ljust(width - 1), width - 1,
                               STYLES[ui.WARN])
                stdscr.move(height - 1, min(len(text), width - 2))
                stdscr.refresh()
                ch = stdscr.getch()
                if ch in (27,):                      # ESC
                    return None
                if ch in (10, 13, curses.KEY_ENTER):
                    value = ''.join(buf).strip()
                    if value or allow_empty:
                        return value
                    return None
                if ch == 21:                         # Ctrl-U
                    buf, pristine = [], False
                    continue
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    if pristine:
                        buf, pristine = [], False
                    elif buf:
                        buf.pop()
                elif 32 <= ch < 127:
                    if pristine:
                        buf, pristine = [], False
                    buf.append(chr(ch))
        finally:
            curses.curs_set(0)

    def confirm(question):
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(height - 1, 0, f" {question} [y/N]".ljust(width - 1), width - 1,
                       STYLES[ui.WARN])
        stdscr.refresh()
        return stdscr.getch() in (ord('y'), ord('Y'))

    def show_modal(lines):
        """Blocking message box — used for a freshly minted API key."""
        nonlocal scroll
        saved_scroll = scroll
        scroll = 0
        paint(lines + [ui._line(), ui._line('  Press any key to continue.', ui.WARN)])
        stdscr.getch()
        scroll = saved_scroll

    def refresh(msg='', style=ui.NORMAL):
        nonlocal state, flash
        state = load_state(screen)
        flash = (msg, style)

    def current_lines():
        if screen == 'Dashboard':
            return ui.render_dashboard(state)
        if screen == 'Accounts':
            if detail_of is not None:
                account = next((a for a in (state['accounts'] or [])
                                if a['id'] == detail_of), None)
                if account:
                    return ui.render_account_detail(
                        account, state.get('settings') or [],
                        (state.get('usage') or {}).get(account['id']),
                        usage.recent(account['id'], limit=12),
                    )
            return ui.render_accounts({**state, 'selected': selected})
        if screen == 'Settings':
            return ui.render_global_settings({**state, 'selected': selected})
        if screen == 'Database':
            return ui.render_schema(state.get('schema') or {})
        if screen == 'Health':
            return ui.render_health(health)
        if screen == 'Browser':
            return ui.render_browser({'browse_tables': browse_tables,
                                      'browse_page': browse_page, 'selected': selected})
        if screen == 'Reports':
            if catalog_detail is not None:
                return ui.render_catalog_detail(catalog_detail)
            return ui.render_catalog({'catalog': state.get('catalog') or [],
                                      'selected': selected})
        if screen == 'Examples':
            return ui.render_examples({'examples': examples_blocks or [],
                                       'examples_for': examples_for})
        return ui.HELP

    def selected_account():
        """The account the next action applies to.

        In the detail view that is the account on screen, not whatever the list
        cursor happens to sit on — otherwise the actions would silently target a
        different row than the one being read.
        """
        rows = state.get('accounts') or []
        if detail_of is not None:
            shown = next((a for a in rows if a['id'] == detail_of), None)
            if shown:
                return shown
        return rows[selected] if 0 <= selected < len(rows) else None

    # -- account actions --------------------------------------------------
    def act_new_account():
        nonlocal flash, detail_of
        detail_of = None          # a new account is not the one being detailed
        name = prompt('Account name')
        if not name:
            return
        types = prompt('Report types (comma separated)', 'team,matchup')
        if types is None:
            return
        wanted = [t.strip() for t in types.split(',') if t.strip()]
        unknown = [t for t in wanted if t not in report_types.REPORT_TYPES]
        if unknown:
            flash = (f"  Unknown report type(s): {', '.join(unknown)}", ui.ERR)
            return
        email = prompt('Contact email (optional)', '', allow_empty=True)
        try:
            account, api_key = accounts.create(name, wanted, contact_email=email or None)
        except Exception as e:
            flash = (f"  Create failed: {e}", ui.ERR)
            return
        show_modal([
            ui._line('NEW API KEY', ui.TITLE),
            ui.rule(),
            ui._line(f"  Account : {account['account_name']} (id {account['id']})"),
            ui._line(f"  Reports : {', '.join(account['allowed_reports'])}"),
            ui._line(),
            ui._line('  This key is shown ONCE and is not recoverable.', ui.WARN),
            ui._line('  Copy it now.', ui.WARN),
            ui._line(),
            ui._line(f"  {api_key}", ui.OK),
        ])
        refresh(f"  Account {account['id']} created.", ui.OK)

    def act_rotate():
        nonlocal flash
        account = selected_account()
        if not account:
            return
        if not confirm(f"Rotate the key for '{account['account_name']}'? "
                       f"The current key stops working immediately."):
            flash = ('  Cancelled.', ui.DIM)
            return
        try:
            account, api_key = accounts.rotate_key(account['id'])
        except Exception as e:
            flash = (f"  Rotate failed: {e}", ui.ERR)
            return
        show_modal([
            ui._line('ROTATED API KEY', ui.TITLE),
            ui.rule(),
            ui._line(f"  Account : {account['account_name']} (id {account['id']})"),
            ui._line(),
            ui._line('  The previous key is now invalid.', ui.WARN),
            ui._line('  This key is shown ONCE.', ui.WARN),
            ui._line(),
            ui._line(f"  {api_key}", ui.OK),
        ])
        refresh('  Key rotated.', ui.OK)

    def act_edit_reports():
        nonlocal flash
        account = selected_account()
        if not account:
            return
        available = ', '.join(sorted(report_types.REPORT_TYPES))
        value = prompt(f'Report types ({available})',
                       ','.join(account['allowed_reports'] or []), allow_empty=True)
        if value is None:
            return
        wanted = [t.strip() for t in value.split(',') if t.strip()]
        unknown = [t for t in wanted if t not in report_types.REPORT_TYPES]
        if unknown:
            flash = (f"  Unknown report type(s): {', '.join(unknown)}", ui.ERR)
            return
        try:
            accounts.update(account['id'], allowed_reports=wanted)
        except Exception as e:
            flash = (f"  Update failed: {e}", ui.ERR)
            return
        refresh(f"  Entitlements updated: {', '.join(wanted) or '(none)'}", ui.OK)

    def act_account_setting():
        nonlocal flash
        account = selected_account()
        if not account:
            return
        key = prompt(f"Setting ({', '.join(config.ACCOUNT_SETTING_KEYS)})")
        if not key:
            return
        current = (account.get('settings') or {}).get(key, '')
        value = prompt(f"Value for {key} (blank clears the override)", str(current),
                       allow_empty=True)
        if value is None:
            return
        try:
            accounts.merge_settings(account['id'], {key: (value or None)})
        except Exception as e:
            flash = (f"  {e}", ui.ERR)
            return
        refresh(f"  {key} {'cleared' if not value else 'set to ' + value}.", ui.OK)

    def act_toggle(field, label):
        nonlocal flash
        account = selected_account()
        if not account:
            return
        new_value = not account[field]
        if field == 'is_admin' and new_value and not confirm(
                f"Grant ADMIN rights to '{account['account_name']}'?"):
            return
        try:
            accounts.update(account['id'], **{field: new_value})
        except Exception as e:
            flash = (f"  Update failed: {e}", ui.ERR)
            return
        refresh(f"  {label} -> {'yes' if new_value else 'no'}", ui.OK)

    def act_delete_account():
        nonlocal flash, selected, detail_of
        account = selected_account()
        if not account:
            return
        typed = prompt(f"Type the account NAME to permanently delete id {account['id']}")
        if typed is None:
            return
        if typed != account['account_name']:
            flash = ('  Name did not match — nothing deleted.', ui.DIM)
            return
        keep = not confirm('Keep this account\'s usage history? [y] keep / [n] purge')
        try:
            accounts.delete(account['id'], purge_usage=keep)
        except Exception as e:
            flash = (f"  Delete failed: {e}", ui.ERR)
            return
        selected = max(0, selected - 1)
        detail_of = None          # the detailed account no longer exists
        refresh(f"  Account {account['id']} deleted"
                f"{' with its usage history' if keep else '; usage history kept'}.", ui.OK)

    def act_clear_watermark():
        nonlocal flash
        account = selected_account()
        if not account or not account.get('watermark_file'):
            flash = ('  That account already uses the service default.', ui.DIM)
            return
        if not confirm(f"Remove the custom watermark for '{account['account_name']}'?"):
            return
        try:
            accounts.clear_watermark(account['id'])
        except Exception as e:
            flash = (f"  Failed: {e}", ui.ERR)
            return
        refresh('  Watermark cleared.', ui.OK)

    # -- settings actions -------------------------------------------------
    def act_set_global():
        nonlocal flash
        rows = state.get('settings') or []
        if not (0 <= selected < len(rows)):
            return
        row = rows[selected]
        value = prompt(f"{row['key']} (blank clears the override)", str(row['value']),
                       allow_empty=True)
        if value is None:
            return
        try:
            if value == '':
                settings_store.clear_value(row['key'])
                msg = f"  {row['key']} reverted to the environment default."
            else:
                settings_store.set_value(row['key'], value)
                msg = f"  {row['key']} set to {value} (live, no restart needed)."
        except Exception as e:
            flash = (f"  {e}", ui.ERR)
            return
        refresh(msg, ui.OK)

    def act_clear_global():
        nonlocal flash
        rows = state.get('settings') or []
        if not (0 <= selected < len(rows)):
            return
        row = rows[selected]
        if not row['overridden']:
            flash = ('  That setting is already the environment default.', ui.DIM)
            return
        try:
            settings_store.clear_value(row['key'])
        except Exception as e:
            flash = (f"  {e}", ui.ERR)
            return
        refresh(f"  {row['key']} reverted to the environment default.", ui.OK)

    def load_browser(source=None):
        nonlocal browse_tables, browse_page, browse_source, selected
        if source:
            browse_source = source
        browse_tables = dbbrowse.list_tables(browse_source)
        browse_page = None
        selected = 0

    def act_open_table():
        nonlocal browse_page
        tables = (browse_tables or {}).get('tables') or []
        if not (0 <= selected < len(tables)):
            return
        browse_page = dbbrowse.read_table(browse_source, tables[selected]['name'])

    def act_browse_page(delta):
        nonlocal browse_page, flash
        if not browse_page:
            return
        total = browse_page.get('total') or 0
        offset = browse_page['offset'] + delta * browse_page['limit']
        if offset < 0 or offset >= max(total, 1):
            flash = ('  No further pages.', ui.DIM)
            return
        browse_page = dbbrowse.read_table(browse_source, browse_page['table'],
                                          offset=offset, limit=browse_page['limit'])

    def act_examples_for_account():
        nonlocal examples_blocks, examples_for, flash
        rows = state.get('accounts') or []
        if not rows:
            flash = ('  No accounts yet.', ui.DIM)
            return
        ident = prompt(f"Account id ({', '.join(str(a['id']) for a in rows)})",
                       str(rows[0]['id']))
        if not ident:
            return
        account = next((a for a in rows if str(a['id']) == ident.strip()), None)
        if not account:
            flash = (f"  No account with id {ident}.", ui.ERR)
            return
        examples_blocks = examples.build(account)
        examples_for = f"{account['account_name']} (id {account['id']})"
        flash = (f"  Examples built for {account['account_name']}.", ui.OK)

    def act_write_examples():
        nonlocal flash
        if not examples_blocks:
            flash = ('  Nothing to write yet.', ui.DIM)
            return
        default = os.path.join(os.path.expanduser('~'), 'afplna_api_examples.sh')
        path = prompt('Write examples to', default)
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(f"# API examples — {examples_for or 'all accounts'}\n\n")
                fh.write(examples.as_text(examples_blocks))
        except Exception as e:
            flash = (f"  Write failed: {e}", ui.ERR)
            return
        flash = (f"  Written to {path}", ui.OK)

    def act_apply_schema():
        nonlocal flash
        report = state.get('schema') or {}
        fixes = report.get('fixes') or []
        if not fixes:
            flash = ('  Nothing to repair.', ui.DIM)
            return
        if not confirm(f"Apply {len(fixes)} additive repair(s) to {report.get('database')}?"):
            return
        results = schema.apply(fixes)
        good = sum(1 for r in results if r['ok'])
        bad = [r for r in results if not r['ok']]
        if bad:
            refresh(f"  {good} applied, {len(bad)} failed: {bad[0]['error'][:60]}", ui.ERR)
        else:
            refresh(f"  {good} repair(s) applied successfully.", ui.OK)

    # -- main loop --------------------------------------------------------
    while True:
        paint(current_lines())
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            return

        flash = ('', ui.NORMAL)

        if ch in (ord('q'), ord('Q')):
            return
        if ch == 27:                                  # ESC
            if detail_of is not None:
                detail_of = None
            elif browse_page is not None:
                browse_page = None
            elif catalog_detail is not None:
                catalog_detail = None
            continue
        if ch in (ord('?'), curses.KEY_F1):
            screen, detail_of, scroll = 'Help', None, 0
            continue
        if ord('1') <= ch <= ord('8'):
            screen = SCREENS[ch - ord('1')]
            detail_of, selected, scroll = None, 0, 0
            catalog_detail, browse_page = None, None
            if screen == 'Health' and not health:
                flash = ('  Press [r] to run the live health checks.', ui.DIM)
            elif screen == 'Browser' and browse_tables is None:
                load_browser()
            elif screen == 'Reports' and not state.get('catalog'):
                try:
                    state['catalog'] = catalog.all_reports()
                except Exception as e:
                    flash = (f'  Catalog failed: {e}', ui.ERR)
            elif screen == 'Examples' and examples_blocks is None:
                examples_blocks = examples.build(None)
                examples_for = 'all report types (generic)'
            continue
        if ch in (ord('r'), ord('R')):
            if screen == 'Health':
                paint([ui._line('  Running health checks (this makes live API calls)...',
                                ui.WARN)])
                health = run_health()
                flash = ('  Health checks complete.', ui.OK)
            elif screen == 'Browser':
                load_browser()
                flash = ('  Reloaded.', ui.DIM)
            else:
                refresh('  Refreshed.', ui.DIM)
                if screen == 'Reports':
                    try:
                        state['catalog'] = catalog.all_reports()
                    except Exception:
                        pass
            continue
        listable = (
            (screen == 'Accounts' and detail_of is None and (state.get('accounts') or [])) or
            (screen == 'Settings' and (state.get('settings') or [])) or
            (screen == 'Browser' and browse_page is None
             and ((browse_tables or {}).get('tables') or [])) or
            (screen == 'Reports' and catalog_detail is None and (state.get('catalog') or []))
        )
        if ch == curses.KEY_UP:
            if listable:
                selected = max(0, selected - 1)
            else:
                scroll = max(0, scroll - 1)
            continue
        if ch == curses.KEY_DOWN:
            if listable:
                selected = min(len(listable) - 1, selected + 1)
            else:
                scroll += 1
            continue
        if ch in (curses.KEY_NPAGE, ord(' ')):
            scroll += 10
            continue
        if ch == curses.KEY_PPAGE:
            scroll = max(0, scroll - 10)
            continue

        if screen == 'Accounts':
            # The actions stay live in the detail view too. Gating them on the list
            # view turned "press ENTER on a row" into a dead end where every key did
            # nothing, with no hint that ESC was the way out.
            if ch in (10, 13, curses.KEY_ENTER):
                account = selected_account()
                detail_of = None if detail_of is not None else (account or {}).get('id')
            elif ch == ord('n'):
                act_new_account()
            elif ch == ord('k'):
                act_rotate()
            elif ch == ord('e'):
                act_edit_reports()
            elif ch == ord('s'):
                act_account_setting()
            elif ch == ord('t'):
                act_toggle('active', 'Active')
            elif ch == ord('m'):
                act_toggle('is_admin', 'Admin')
            elif ch == ord('w'):
                act_clear_watermark()
            elif ch == ord('D'):
                act_delete_account()
        elif screen == 'Browser':
            if ch in (10, 13, curses.KEY_ENTER):
                act_open_table()
            elif ch == ord('b'):
                load_browser(dbbrowse.ROTOWIRE if browse_source == dbbrowse.MYSQL
                             else dbbrowse.MYSQL)
            elif ch == ord('n'):
                act_browse_page(1)
            elif ch == ord('p'):
                act_browse_page(-1)
        elif screen == 'Reports':
            if ch in (10, 13, curses.KEY_ENTER):
                if catalog_detail is not None:
                    catalog_detail = None
                else:
                    rows = state.get('catalog') or []
                    if 0 <= selected < len(rows):
                        catalog_detail = rows[selected]
                        scroll = 0
        elif screen == 'Examples':
            if ch == ord('a'):
                act_examples_for_account()
                scroll = 0
            elif ch == ord('d'):
                examples_blocks = examples.build_admin()
                examples_for = 'administrator calls'
                scroll = 0
            elif ch == ord('w'):
                act_write_examples()
        elif screen == 'Settings':
            if ch in (10, 13, curses.KEY_ENTER):
                act_set_global()
            elif ch == ord('x'):
                act_clear_global()
        elif screen == 'Database':
            if ch == ord('a'):
                act_apply_schema()


def run_tui() -> int:
    db_error = preflight()
    if db_error:
        print(f"Database unreachable: {db_error}\n", file=sys.stderr)
        print_env_guidance()
        return 3
    try:
        import curses
    except ImportError:
        print("curses is unavailable in this Python build. Use the CLI subcommands "
              "instead — run: admin_tui.py --help", file=sys.stderr)
        return 2
    if not sys.stdout.isatty():
        print("Not a terminal. Use the CLI subcommands — run: admin_tui.py --help",
              file=sys.stderr)
        return 2
    try:
        curses.wrapper(_curses_main)
        return 0
    except Exception as e:
        # curses leaves the terminal in a mess if it dies mid-draw; wrapper restores it,
        # but the traceback still needs to be legible.
        print(f"Console failed: {e.__class__.__name__}: {e}", file=sys.stderr)
        print("Fall back to the CLI subcommands — run: admin_tui.py --help", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _plain(lines) -> str:
    return "\n".join(text for text, _style in lines)


def main(argv: list[str]) -> int:
    if not argv:
        return run_tui()

    cmd, args = argv[0], argv[1:]

    if cmd in ('-h', '--help', 'help'):
        print(__doc__)
        return 0

    if cmd == 'env':
        print(f"Environment source: {ENV_REPORT.get('source') or 'NOT FOUND'}")
        if ENV_REPORT.get('loaded'):
            print(f"Imported: {', '.join(ENV_REPORT['loaded'])}")
        for attempt in ENV_REPORT.get('attempts', []):
            print(f"  tried {attempt}")
        if ENV_REPORT.get('missing'):
            print()
            print_env_guidance()
            return 1
        return 0

    # Every remaining command touches the database. If it is unreachable AND the
    # service environment never loaded, that is almost certainly the cause — say so
    # instead of surfacing "Access denied ... (using password: NO)".
    db_error = preflight()
    if db_error:
        print(f"Database unreachable: {db_error}\n", file=sys.stderr)
        print_env_guidance()
        return 3

    if cmd == 'audit':
        report = schema.audit()
        print(_plain(ui.render_schema(report)))
        if '--apply' in args:
            fixes = report.get('fixes') or []
            if not fixes:
                print("\nNothing to apply.")
                return 0
            results = schema.apply(fixes)
            print()
            for r in results:
                print(f"  {'OK  ' if r['ok'] else 'FAIL'} {r['describe']}"
                      f"{'' if r['ok'] else ' — ' + str(r['error'])}")
            return 0 if all(r['ok'] for r in results) else 1
        return 0 if report.get('ok') else 1

    if cmd == 'accounts':
        try:
            rows = accounts.list_all()
        except Exception as e:
            print(f"Could not load accounts: {e}", file=sys.stderr)
            return 1
        print(_plain(ui.render_accounts({'accounts': rows, 'selected': -1})))
        return 0

    if cmd == 'create':
        if not args:
            print("usage: admin_tui.py create NAME [report,types] [email]", file=sys.stderr)
            return 2
        name = args[0]
        types = [t.strip() for t in (args[1] if len(args) > 1 else 'team,matchup').split(',')
                 if t.strip()]
        unknown = [t for t in types if t not in report_types.REPORT_TYPES]
        if unknown:
            print(f"Unknown report type(s): {', '.join(unknown)}. "
                  f"Available: {', '.join(sorted(report_types.REPORT_TYPES))}", file=sys.stderr)
            return 2
        account, api_key = accounts.create(name, types,
                                           contact_email=args[2] if len(args) > 2 else None)
        print(f"Account {account['id']} created: {account['account_name']}")
        print(f"Report types: {', '.join(account['allowed_reports'])}")
        print("\nAPI key (shown once, not recoverable):\n")
        print(f"  {api_key}\n")
        return 0

    if cmd == 'rotate':
        if not args:
            print("usage: admin_tui.py rotate ACCOUNT_ID", file=sys.stderr)
            return 2
        account, api_key = accounts.rotate_key(int(args[0]))
        print(f"Key rotated for account {account['id']} ({account['account_name']}).")
        print("The previous key is now invalid.\n")
        print(f"  {api_key}\n")
        return 0

    if cmd == 'settings':
        print(_plain(ui.render_global_settings({'settings': settings_store.describe(),
                                                'selected': -1})))
        return 0

    if cmd == 'set':
        if len(args) < 2:
            print("usage: admin_tui.py set KEY VALUE", file=sys.stderr)
            return 2
        try:
            settings_store.set_value(args[0], args[1])
        except Exception as e:
            print(f"{e}", file=sys.stderr)
            return 2
        print(f"{args[0]} = {args[1]}  (live, no restart needed)")
        return 0

    if cmd == 'unset':
        if not args:
            print("usage: admin_tui.py unset KEY", file=sys.stderr)
            return 2
        settings_store.clear_value(args[0])
        print(f"{args[0]} reverted to the environment default.")
        return 0

    if cmd == 'delete':
        if not args:
            print("usage: admin_tui.py delete ACCOUNT_ID [--purge-usage]", file=sys.stderr)
            return 2
        account = accounts.get(int(args[0]))
        if not account:
            print(f"No account with id {args[0]}", file=sys.stderr)
            return 1
        confirmed = input(f"Type the account name to delete '{account['account_name']}': ")
        if confirmed.strip() != account['account_name']:
            print("Name did not match — nothing deleted.", file=sys.stderr)
            return 1
        accounts.delete(account['id'], purge_usage='--purge-usage' in args)
        print(f"Account {account['id']} ({account['account_name']}) deleted.")
        return 0

    if cmd == 'usage':
        if args:
            account_id = int(args[0])
            account = accounts.get(account_id)
            if not account:
                print(f"No account with id {account_id}", file=sys.stderr)
                return 1
            print(_plain(ui.render_account_detail(
                account, settings_store.describe(),
                usage.summary_by_account().get(account_id),
                usage.recent(account_id, limit=25))))
            return 0
        totals = usage.totals()
        print(f"Requests: {totals['requests']}  done: {totals['done']}  "
              f"errors: {totals['errors']}  last 30d: {totals['last_30d']}")
        print()
        summary = usage.summary_by_account()
        print(f"  {'ID':<5} {'ACCOUNT':<28} {'TOTAL':>7} {'DONE':>6} {'ERR':>5} {'30D':>5}")
        for account in accounts.list_all():
            stats = summary.get(account['id']) or {}
            print(f"  {account['id']:<5} {account['account_name'][:27]:<28} "
                  f"{stats.get('total', 0):>7} {stats.get('done', 0):>6} "
                  f"{stats.get('error', 0):>5} {stats.get('last_30d', 0):>5}")
        return 0

    if cmd == 'tables':
        source = dbbrowse.ROTOWIRE if args and args[0].startswith('roto') else dbbrowse.MYSQL
        listing = dbbrowse.list_tables(source)
        print(_plain(ui.render_browser({'browse_tables': listing, 'selected': -1})))
        return 0 if listing.get('ok') else 1

    if cmd == 'show':
        if not args:
            print("usage: admin_tui.py show TABLE [OFFSET] [rotowire]", file=sys.stderr)
            return 2
        source = dbbrowse.ROTOWIRE if 'rotowire' in args[1:] else dbbrowse.MYSQL
        offset = 0
        for extra in args[1:]:
            if extra.isdigit():
                offset = int(extra)
        page = dbbrowse.read_table(source, args[0], offset=offset)
        print(_plain(ui.render_browser({'browse_tables': dbbrowse.list_tables(source),
                                        'browse_page': page})))
        return 0 if page.get('ok') else 1

    if cmd == 'reports':
        if args:
            try:
                print(_plain(ui.render_catalog_detail(catalog.describe(args[0]))))
            except report_types.ValidationError as e:
                print(str(e), file=sys.stderr)
                return 2
            return 0
        print(_plain(ui.render_catalog({'catalog': catalog.all_reports(), 'selected': -1})))
        return 0

    if cmd == 'examples':
        if args and args[0] == 'admin':
            print(examples.as_text(examples.build_admin()))
            return 0
        account = None
        if args:
            account = accounts.get(int(args[0]))
            if not account:
                print(f"No account with id {args[0]}", file=sys.stderr)
                return 1
        print(examples.as_text(examples.build(account)))
        return 0

    if cmd == 'health':
        print(_plain(ui.render_health(run_health())))
        return 0

    print(f"Unknown command '{cmd}'. Run: admin_tui.py --help", file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
