"""Where generated PDFs live, and who owns them.

Every report used to land in one flat REPORTS_DIR keyed only by subject and date, so
two accounts asking for the same matchup on the same day wrote the same filename and
silently overwrote each other. Files are now partitioned by account:

    REPORTS_DIR/                    the legacy AFPLNA flow, unchanged
    REPORTS_DIR/account_3/          everything account 3 generates through /v1

The legacy endpoints glob the root, which never matches a subdirectory, so that flow
keeps behaving exactly as before while /v1 gets real isolation.

Nothing here trusts a caller-supplied filename: every path is resolved and checked to
be inside the account's own directory before it is read or unlinked.
"""

import logging
import os
import re
import shutil
from datetime import datetime

import config

LEGACY = 'legacy'          # pseudo-account for the AFPLNA flow at the root
PREFIX = 'account_'


def account_dir(account_id: int | None, create: bool = True) -> str:
    """Directory for one account's reports. None means the legacy root."""
    if account_id is None:
        path = config.REPORTS_DIR
    else:
        path = os.path.join(config.REPORTS_DIR, f'{PREFIX}{int(account_id)}')
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _safe_name(filename: str) -> str:
    """Reject anything that is not a plain file name."""
    raw = (filename or '').strip()
    name = os.path.basename(raw)
    # os.altsep is None on POSIX, and `'' in name` is true for every string — so the
    # separator check has to skip it rather than fold it in with `or ''`.
    separators = [os.sep] + ([os.altsep] if os.altsep else [])
    if not name or name.startswith('.') or any(sep in raw for sep in separators):
        raise ValueError(f"Unsafe report filename: {filename!r}")
    return name


def resolve(account_id: int | None, filename: str) -> str:
    """Absolute path to one report, guaranteed to sit inside that account's directory."""
    base = os.path.realpath(account_dir(account_id, create=False))
    path = os.path.realpath(os.path.join(base, _safe_name(filename)))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError(f"Report path escapes its account directory: {filename!r}")
    return path


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _describe(path: str, name: str) -> dict:
    try:
        stat = os.stat(path)
        size, modified = stat.st_size, datetime.fromtimestamp(stat.st_mtime)
    except OSError:
        size, modified = 0, None
    # Matchup filenames are "{home}_vs_{away}_{game date}_{Month D, YYYY}.pdf"
    # (older files joined the schools with a bare underscore); every other type is
    # a prefix followed by the subject: "team_...", "plays_...", "recap_...",
    # "slate_...", "wrap_...", "predaudit_...", "predreview_...".
    stem = name[:-4] if name.lower().endswith('.pdf') else name
    prefixes = (
        ('team_', 'team'),
        ('plays_', 'season_plays'),
        ('recap_', 'full_game_recap'),
        ('slate_', 'weekly_preview'),
        ('wrap_', 'weekly_wrap'),
        ('predaudit_', 'prediction_audit'),
        ('predreview_', 'prediction_review'),
    )
    for prefix, rtype in prefixes:
        if stem.startswith(prefix):
            report_type, subject = rtype, stem[len(prefix):]
            break
    else:
        report_type, subject = 'matchup', stem
    subject = re.sub(r'_[A-Z][a-z]+ \d{1,2}, \d{4}$', '', subject)
    # Matchup and recap filenames carry the GAME date (ISO) before the generation
    # date, because the same teams can meet twice a season. Split it out so the
    # subject stays clean and consumers get an explicit identifier.
    game_date = None
    m = re.search(r'_(\d{4}-\d{2}-\d{2})$', subject)
    if m:
        game_date = m.group(1)
        subject = subject[:m.start()]
    subject = subject.replace('_', ' ')
    return {
        'filename': name,
        'path': path,
        'bytes': size,
        'modified': modified,
        'report_type': report_type,
        'subject': subject.strip() or stem,
        'game_date': game_date,
    }


def list_reports(account_id: int | None) -> list[dict]:
    """One account's reports, newest first. Never raises."""
    path = account_dir(account_id, create=False)
    if not os.path.isdir(path):
        return []
    out = []
    try:
        for name in os.listdir(path):
            if not name.lower().endswith('.pdf'):
                continue          # skips .building temp files by construction
            full = os.path.join(path, name)
            if os.path.isfile(full):
                out.append(_describe(full, name))
    except OSError as e:
        logging.warning(f"Could not list reports in {path}: {e}")
    out.sort(key=lambda r: r['modified'] or datetime.min, reverse=True)
    return out


def known_account_ids() -> list[int]:
    """Account ids that have a report directory on disk, whether or not they still exist."""
    ids = []
    try:
        for name in os.listdir(config.REPORTS_DIR):
            if name.startswith(PREFIX) and os.path.isdir(
                    os.path.join(config.REPORTS_DIR, name)):
                try:
                    ids.append(int(name[len(PREFIX):]))
                except ValueError:
                    continue
    except OSError:
        pass
    return sorted(ids)


def overview(accounts_list: list[dict] | None = None) -> list[dict]:
    """Per-account totals, including directories whose account has been deleted."""
    by_id = {a['id']: a for a in (accounts_list or [])}
    rows = []

    legacy = list_reports(None)
    rows.append({
        'account_id': None,
        'account_name': 'AFPLNA (legacy flow)',
        'reports': len(legacy),
        'bytes': sum(r['bytes'] for r in legacy),
        'newest': legacy[0]['modified'] if legacy else None,
        'orphaned': False,
    })

    for account_id in sorted(set(known_account_ids()) | set(by_id)):
        reports = list_reports(account_id)
        account = by_id.get(account_id)
        rows.append({
            'account_id': account_id,
            'account_name': (account or {}).get('account_name')
                            or f'(deleted account {account_id})',
            'reports': len(reports),
            'bytes': sum(r['bytes'] for r in reports),
            'newest': reports[0]['modified'] if reports else None,
            'orphaned': account is None,
        })
    return rows


def totals() -> dict:
    rows = overview()
    return {
        'accounts': len(rows),
        'reports': sum(r['reports'] for r in rows),
        'bytes': sum(r['bytes'] for r in rows),
    }


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------
def delete(account_id: int | None, filename: str) -> dict:
    """Remove one report. Raises ValueError if the path is not that account's."""
    path = resolve(account_id, filename)
    if not os.path.isfile(path):
        return {'ok': False, 'filename': filename, 'error': 'No such report'}
    size = os.path.getsize(path)
    os.remove(path)
    logging.info(f"Deleted report {path} (account {account_id})")
    return {'ok': True, 'filename': filename, 'bytes': size}


def clear(account_id: int | None, remove_dir: bool = False) -> dict:
    """Delete every report for one account.

    The legacy root is never removed as a directory — the service writes into it — and
    only its .pdf files are touched, so account subdirectories are left alone.
    """
    path = account_dir(account_id, create=False)
    out = {'account_id': account_id, 'deleted': 0, 'bytes': 0, 'errors': []}
    if not os.path.isdir(path):
        return out

    for report in list_reports(account_id):
        try:
            os.remove(report['path'])
            out['deleted'] += 1
            out['bytes'] += report['bytes']
        except OSError as e:
            out['errors'].append(f"{report['filename']}: {e}")

    if remove_dir and account_id is not None and not out['errors']:
        try:
            shutil.rmtree(path)
            out['removed_dir'] = True
        except OSError as e:
            out['errors'].append(f"Could not remove {path}: {e}")

    logging.info(f"Cleared {out['deleted']} reports for account {account_id}")
    return out
