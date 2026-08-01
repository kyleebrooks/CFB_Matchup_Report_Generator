"""Copy-pasteable API examples, generated per account.

The samples are built from the live registry and the account's real entitlements, so
what you copy is what that key can actually do — no placeholder that turns out to be
wrong, and no example for a report the key is not allowed to request.

Keys are never included. Examples reference $KEY, with the export line shown once at
the top, so an example can be pasted into a shared channel without leaking anything.
"""

import json

import config
import report_types

DEFAULT_HOST = 'http://143.198.20.72'


def _curl(method: str, path: str, host: str, body: dict | None = None,
          form: str | None = None) -> list[str]:
    lines = [f'curl -sS -X {method} {host}{path} \\', '  -H "X-Api-Key: $KEY" \\']
    if form:
        lines.append(f'  {form}')
    elif body is not None:
        lines.append("  -H 'Content-Type: application/json' \\")
        payload = json.dumps(body, indent=2)
        indented = payload.replace('\n', '\n  ')
        lines.append(f"  -d '{indented}'")
    else:
        lines[-1] = '  -H "X-Api-Key: $KEY"'
    if lines[-1].endswith(' \\'):
        lines[-1] = lines[-1][:-2]
    return lines


def _sample_params(report_type: str) -> dict:
    if report_type == 'team':
        return {'report_type': 'team', 'team_short': 'Georgia',
                'team_full': 'Georgia Bulldogs', 'year': 2025}
    if report_type == 'matchup':
        return {'report_type': 'matchup',
                'home_full': 'Georgia Bulldogs', 'away_full': 'Marshall Thundering Herd',
                'home_short': 'Georgia', 'away_short': 'Marshall', 'year': 2025}
    spec = report_types.REPORT_TYPES.get(report_type, {})
    body = {'report_type': report_type}
    for name in spec.get('required', []):
        body[name] = f'<{name}>'
    return body


def build(account: dict | None = None, host: str | None = None) -> list[dict]:
    """Return [{title, note, lines}] — every call this account can make."""
    host = (host or DEFAULT_HOST).rstrip('/')
    allowed = list((account or {}).get('allowed_reports') or sorted(report_types.REPORT_TYPES))
    name = (account or {}).get('account_name', 'this account')

    blocks: list[dict] = [{
        'title': 'Set up',
        'note': f'Every example below uses $KEY. Export the API key for {name} once.',
        'lines': [
            '# Paste the key you were given when the account was created or rotated.',
            'export KEY=cfbr_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            f'export HOST={host}',
            '',
            '# The key can also be sent as a bearer token or a query parameter:',
            '#   -H "Authorization: Bearer $KEY"',
            '#   ?api_key=$KEY',
        ],
    }, {
        'title': 'What can this key request?',
        'note': 'Lists every report type, flagged with whether this key is entitled to it.',
        'lines': _curl('GET', '/v1/report-types', '$HOST'),
    }]

    for report_type in allowed:
        if report_type not in report_types.REPORT_TYPES:
            continue
        spec = report_types.REPORT_TYPES[report_type]
        blocks.append({
            'title': f'Generate a {report_type} report',
            'note': (f"{spec['title']}. Returns 202 immediately with a job_id — "
                     f"generation takes 3-6 minutes. Required: "
                     f"{', '.join(spec['required'])}."),
            'lines': _curl('POST', '/v1/reports', '$HOST', _sample_params(report_type)),
        })

    blocks += [{
        'title': 'Poll progress',
        'note': 'Every ~4 seconds. state goes queued -> running -> done | error.',
        'lines': _curl('GET', '/v1/reports/$JOB_ID', '$HOST'),
    }, {
        'title': 'Download the finished PDF',
        'note': '409 if the report is not ready yet; 410 if the file has been swept.',
        'lines': ['curl -sS -o report.pdf \\',
                  '  -H "X-Api-Key: $KEY" \\',
                  '  $HOST/v1/reports/$JOB_ID/download'],
    }, {
        'title': 'Generate and wait, end to end',
        'note': 'A complete shell loop: submit, poll until done, download.',
        'lines': [
            'JOB=$(curl -sS -X POST $HOST/v1/reports \\',
            '  -H "X-Api-Key: $KEY" -H \'Content-Type: application/json\' \\',
            f"  -d '{json.dumps(_sample_params(allowed[0] if allowed else 'team'))}' \\",
            '  | jq -r .job_id)',
            '',
            'while true; do',
            '  S=$(curl -sS "$HOST/v1/reports/$JOB" -H "X-Api-Key: $KEY")',
            '  echo "$S" | jq -r \'"\\(.percent)% \\(.message)"\'',
            '  ST=$(echo "$S" | jq -r .state)',
            '  [ "$ST" = "done" ]  && break',
            '  [ "$ST" = "error" ] && { echo "$S" | jq -r \'.error, .detail\'; exit 1; }',
            '  sleep 4',
            'done',
            '',
            'curl -sS -o report.pdf "$HOST/v1/reports/$JOB/download" -H "X-Api-Key: $KEY"',
        ],
    }, {
        'title': 'Job history for this key',
        'note': 'Reports run during the current service lifetime (cleared on restart).',
        'lines': _curl('GET', '/v1/reports', '$HOST'),
    }, {
        'title': 'Account details and effective settings',
        'note': "'settings' are this account's overrides; 'effective_settings' is what runs.",
        'lines': _curl('GET', '/v1/account', '$HOST'),
    }, {
        'title': 'Usage / call count',
        'note': 'Total requests, completions, failures, last 30 days, and recent history.',
        'lines': _curl('GET', '/v1/account/usage', '$HOST'),
    }, {
        'title': 'Change models or search depth',
        'note': ('Only the keys you send change. Send a key as null to drop the override '
                 'and fall back to the service default.'),
        'lines': _curl('PATCH', '/v1/account/settings', '$HOST', {
            'research_model': config.OPENROUTER_RESEARCH_MODEL,
            'report_model': config.OPENROUTER_REPORT_MODEL,
            'search_max_results': 8,
            'report_effort': 'high',
        }),
    }, {
        'title': 'Revert one setting to the service default',
        'note': 'null clears the override.',
        'lines': _curl('PATCH', '/v1/account/settings', '$HOST',
                       {'search_max_results': None}),
    }, {
        'title': 'Upload a watermark (multipart)',
        'note': 'PNG, JPEG or WebP, 5 MB max. Applies to every report from then on.',
        'lines': _curl('POST', '/v1/account/watermark', '$HOST',
                       form='-F "file=@logo.png"'),
    }, {
        'title': 'Upload a watermark (base64 JSON)',
        'note': 'For clients that cannot do multipart.',
        'lines': [
            'curl -sS -X POST $HOST/v1/account/watermark \\',
            '  -H "X-Api-Key: $KEY" -H \'Content-Type: application/json\' \\',
            '  -d "{\\"image_base64\\":\\"$(base64 -w0 logo.png)\\",'
            '\\"content_type\\":\\"image/png\\"}"',
        ],
    }, {
        'title': 'Check / remove the watermark',
        'note': 'GET returns metadata; add ?download=1 for the image. DELETE reverts.',
        'lines': _curl('GET', '/v1/account/watermark', '$HOST')
                 + [''] + _curl('DELETE', '/v1/account/watermark', '$HOST'),
    }]

    return blocks


def build_admin(host: str | None = None) -> list[dict]:
    """Admin-key examples. Kept separate — these mint and revoke credentials."""
    host = (host or DEFAULT_HOST).rstrip('/')
    types = sorted(report_types.REPORT_TYPES)
    return [{
        'title': 'Set up (administrator)',
        'note': 'ADMIN_API_KEY comes from /etc/afplna.env. It can mint accounts — keep it secret.',
        'lines': ['export ADMIN=your-admin-api-key', f'export HOST={host}'],
    }, {
        'title': 'Create an account',
        'note': ('The api_key in the response is shown ONCE and cannot be recovered — only '
                 'rotated. allowed_reports defaults to all current types.'),
        'lines': [
            'curl -sS -X POST $HOST/v1/admin/accounts \\',
            '  -H "X-Api-Key: $ADMIN" -H \'Content-Type: application/json\' \\',
            "  -d '" + json.dumps({
                'account_name': 'CFBReports.com',
                'contact_email': 'ops@cfbreports.com',
                'allowed_reports': types,
                'settings': {'search_max_results': 8},
            }, indent=2).replace('\n', '\n  ') + "'",
        ],
    }, {
        'title': 'List all accounts',
        'note': 'Key prefixes only — full keys are never returned by any endpoint.',
        'lines': ['curl -sS $HOST/v1/admin/accounts -H "X-Api-Key: $ADMIN"'],
    }, {
        'title': 'Grant or remove report types',
        'note': 'Send the complete list you want the account to end up with.',
        'lines': [
            'curl -sS -X PATCH $HOST/v1/admin/accounts/2 \\',
            '  -H "X-Api-Key: $ADMIN" -H \'Content-Type: application/json\' \\',
            f"  -d '{json.dumps({'allowed_reports': types})}'",
        ],
    }, {
        'title': 'Rotate an account key',
        'note': 'The previous key stops working immediately. The new one is shown once.',
        'lines': ['curl -sS -X POST $HOST/v1/admin/accounts/2/rotate-key \\',
                  '  -H "X-Api-Key: $ADMIN"'],
    }, {
        'title': 'Deactivate an account',
        'note': 'Soft delete — the key stops authenticating but history is preserved.',
        'lines': ['curl -sS -X DELETE $HOST/v1/admin/accounts/2 \\',
                  '  -H "X-Api-Key: $ADMIN"'],
    }]


def as_text(blocks: list[dict]) -> str:
    """Flatten to plain text for a file or a pipe."""
    out = []
    for block in blocks:
        out.append('#' + '=' * 74)
        out.append(f"# {block['title']}")
        if block.get('note'):
            out.append(f"# {block['note']}")
        out.append('#' + '=' * 74)
        out.extend(block['lines'])
        out.append('')
    return '\n'.join(out)
