"""Load the service environment when running outside systemd.

The systemd unit feeds the app /etc/afplna.env via EnvironmentFile=. An interactive
SSH shell gets none of that, so anything run by hand — the admin console, an import
smoke test — starts with no DB_PASSWORD and fails with:

    Access denied for user 'kdogg4207'@'...' (using password: NO)

Three sources are tried, in order:

  1. The environment file, if this user can read it. It is normally chmod 600
     root:root, so this works under sudo and not otherwise.
  2. The running service's own environment, read from /proc/<pid>/environ. The
     service runs as `deploy`, so `deploy` can read it without sudo and without
     relaxing any file permissions. This is usually the one that fires.
  3. Nothing — the caller gets a list of what is still missing and can say so
     precisely instead of surfacing a confusing database error.

Variables already present in the environment are never overwritten: an explicit
`DB_HOST=... admin_tui.py` on the command line still wins.
"""

import os
import shutil
import subprocess

DEFAULT_ENV_FILES = ('/etc/afplna.env', '/etc/default/afplna')
DEFAULT_UNIT = 'afplna'

# Without these the service cannot reach the database at all.
REQUIRED = ('DB_PASSWORD',)
# Worth importing when available, but not fatal.
INTERESTING = (
    'DB_HOST', 'DB_USER', 'DB_NAME', 'DB_PASSWORD',
    'SERVICE_API_KEY', 'ADMIN_API_KEY', 'OPENROUTER_API_KEY', 'CFBD_API_KEY',
    'REPORTS_DIR', 'WATERMARKS_DIR', 'ROTOWIRE_DB_PATH', 'WKHTMLTOPDF_PATH',
    'OPENROUTER_RESEARCH_MODEL', 'OPENROUTER_REPORT_MODEL',
    'OPENROUTER_SEARCH_ENGINE', 'OPENROUTER_SEARCH_MAX_RESULTS',
    'RESEARCH_TIMEOUT', 'REPORT_TIMEOUT', 'REPORT_MAX_TOKENS', 'REPORT_EFFORT',
    'MPLCONFIGDIR',
)


def parse_env_text(text: str) -> dict:
    """Parse systemd EnvironmentFile syntax.

    systemd strips one layer of matching surrounding quotes, so `KEY='v'` and `KEY=v`
    are the same value. We match that, or the console would read a password with
    literal quotes around it and fail just as opaquely as having none.
    """
    out = {}
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key.startswith('export '):
            key = key[7:].strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def from_file(path: str) -> tuple[dict, str | None]:
    """Read an environment file. Returns ({}, reason) when it cannot be read."""
    if not os.path.exists(path):
        return {}, 'not found'
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            return parse_env_text(fh.read()), None
    except PermissionError:
        return {}, 'permission denied'
    except Exception as e:
        return {}, f'{e.__class__.__name__}: {e}'


def from_service(unit: str = DEFAULT_UNIT) -> tuple[dict, str | None]:
    """Read the live environment of the running service via /proc/<pid>/environ."""
    if not shutil.which('systemctl'):
        return {}, 'systemctl not on PATH'
    try:
        pid = subprocess.run(
            ['systemctl', 'show', unit, '-p', 'MainPID', '--value'],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception as e:
        return {}, f'systemctl failed: {e}'

    if not pid or pid == '0':
        return {}, f'{unit} is not running'

    proc_path = f'/proc/{pid}/environ'
    try:
        with open(proc_path, 'rb') as fh:
            blob = fh.read()
    except PermissionError:
        return {}, f'cannot read {proc_path} (different user)'
    except FileNotFoundError:
        return {}, f'{proc_path} disappeared'
    except Exception as e:
        return {}, f'{e.__class__.__name__}: {e}'

    out = {}
    for chunk in blob.split(b'\0'):
        if not chunk or b'=' not in chunk:
            continue
        key, _, value = chunk.decode('utf-8', 'replace').partition('=')
        out[key] = value
    return out, None


def bootstrap(
    required: tuple = REQUIRED,
    env_files: tuple = DEFAULT_ENV_FILES,
    unit: str = DEFAULT_UNIT,
    environ: dict | None = None,
) -> dict:
    """Fill in missing service variables. Returns a report of what happened."""
    environ = os.environ if environ is None else environ
    report = {'source': None, 'loaded': [], 'missing': [], 'attempts': []}

    if all(environ.get(name) for name in required):
        report['source'] = 'environment'
        return report

    candidates = [(f'file:{p}', lambda p=p: from_file(p)) for p in env_files]
    candidates.append((f'service:{unit}', lambda: from_service(unit)))

    for label, fetch in candidates:
        values, problem = fetch()
        if problem:
            report['attempts'].append(f'{label}: {problem}')
            continue
        if not values:
            report['attempts'].append(f'{label}: empty')
            continue

        applied = []
        for key in INTERESTING:
            # Never clobber something the caller set explicitly.
            if key in values and not environ.get(key):
                environ[key] = values[key]
                applied.append(key)

        if all(environ.get(name) for name in required):
            report['source'] = label
            report['loaded'] = applied
            return report
        report['attempts'].append(f'{label}: read, but still missing required values')
        report['loaded'] = applied

    report['missing'] = [name for name in required if not environ.get(name)]
    return report


def guidance(report: dict, unit: str = DEFAULT_UNIT) -> list[str]:
    """Actionable next steps when bootstrap could not find the environment."""
    if not report.get('missing'):
        return []
    lines = [
        f"Missing required environment: {', '.join(report['missing'])}",
        '',
        'The service gets these from /etc/afplna.env via systemd. An interactive shell',
        'does not, so they have to be supplied one of these ways:',
        '',
        '  1. Run under sudo, which can read the env file:',
        '       sudo /opt/afplna/venv/bin/python /opt/afplna/admin_tui.py',
        '',
        f'  2. Start the service, so its environment can be read from /proc:',
        f'       sudo systemctl start {unit}',
        '',
        '  3. Let the deploy user read the env file directly (one-time):',
        '       sudo chown root:deploy /etc/afplna.env',
        '       sudo chmod 640 /etc/afplna.env',
        '',
        '  4. Or export it for this shell only:',
        '       set -a; source /etc/afplna.env; set +a',
    ]
    if report.get('attempts'):
        lines += ['', 'Tried:']
        lines += [f'  - {a}' for a in report['attempts']]
    return lines
