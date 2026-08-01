"""Multi-tenant REST API (/v1) for CFBReports.com and other consumers.

Entirely separate from the legacy AFPLNA endpoints: /generate-report, /report-status,
/get-report and /has-report keep authenticating with SERVICE_API_KEY and behave exactly
as before. Everything here authenticates with a per-account key issued by an admin.
"""

import base64
import functools
import logging
import os

from flask import Blueprint, jsonify, request, send_file

import accounts
import config
import jobs
import report_types
import reports_store
import usage

bp = Blueprint('api_v1', __name__, url_prefix='/v1')


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _presented_key() -> str | None:
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip()
    header = request.headers.get('X-Api-Key')
    if header:
        return header.strip()
    body = request.get_json(silent=True) or {}
    return request.args.get('api_key') or body.get('api_key')


def _error(message: str, status: int = 400, **extra):
    payload = {'error': message}
    payload.update(extra)
    return jsonify(payload), status


def require_account(fn):
    """Resolve the caller's account, or 401."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = _presented_key()
        if not key:
            return _error('Missing API key. Send it as "X-Api-Key" or "Authorization: Bearer".', 401)
        try:
            account = accounts.find_by_key(key)
        except Exception as e:
            logging.exception("Account lookup failed")
            return _error('Account lookup failed', 503, detail=str(e)[:200])
        if not account:
            return _error('Invalid or disabled API key.', 401)
        request.account = account
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    """Admin endpoints accept the env bootstrap key or any is_admin account."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = _presented_key()
        if not key:
            return _error('Missing API key.', 401)
        try:
            if not accounts.is_admin_key(key):
                return _error('Administrator privileges required.', 403)
        except Exception as e:
            logging.exception("Admin check failed")
            return _error('Account lookup failed', 503, detail=str(e)[:200])
        request.account = accounts.find_by_key(key)
        return fn(*args, **kwargs)
    return wrapper


def _body() -> dict:
    return request.get_json(silent=True) or request.form.to_dict(flat=True) or {}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@bp.route('/report-types', methods=['GET'])
@require_account
def report_type_catalog():
    """Every report type, flagged with whether this key may request it."""
    allowed = set(request.account['allowed_reports'] or [])
    catalog = []
    for entry in report_types.catalog():
        entry = dict(entry)
        entry['allowed'] = entry['report_type'] in allowed
        catalog.append(entry)
    return jsonify({
        'report_types': catalog,
        'allowed_reports': sorted(allowed),
    }), 200


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@bp.route('/reports', methods=['POST'])
@require_account
def create_report():
    """Queue a report. Returns 202 with a job handle; poll GET /v1/reports/{job_id}."""
    account = request.account
    data = _body()
    report_type = (data.get('report_type') or '').strip().lower()
    if not report_type:
        return _error(
            "'report_type' is required.",
            400,
            available=sorted(report_types.REPORT_TYPES),
        )

    try:
        spec = report_types.get(report_type)
    except report_types.ValidationError as e:
        return _error(str(e), 400)

    if report_type not in (account['allowed_reports'] or []):
        return _error(
            f"This API key is not entitled to the '{report_type}' report.",
            403,
            allowed_reports=account['allowed_reports'],
        )

    try:
        params = spec['validate'](data)
    except report_types.ValidationError as e:
        return _error(str(e), 400, required_params=spec['required'])

    # Per-account model/search choices, watermark and output directory travel with the
    # job. The directory is what keeps two customers' PDFs apart: filenames are built
    # from subject and date, so without it the same matchup on the same day collides.
    params['settings'] = accounts.effective_settings(account)
    params['watermark'] = accounts.watermark_path(account)
    params['report_dir'] = reports_store.account_dir(account['id'])

    subject = spec['subject'](params)

    # Record the request BEFORE submitting: submit() starts the worker immediately, so
    # anything assigned afterwards can be read by the running job before it exists.
    usage_row = usage.record_request(account['id'], report_type, subject, '')

    # Wrap the builder so every request is accounted for, start and finish, without the
    # report pipelines needing to know anything about billing.
    def tracked(job_params, progress, _spec=spec, _row=usage_row):
        try:
            result = _spec['run'](job_params, progress)
        except Exception as e:
            usage.mark_complete(_row, 'error', error=f"{e.__class__.__name__}: {e}")
            raise
        usage.mark_complete(_row, 'done', seconds=result.get('seconds'),
                            sources=result.get('sources'))
        return result

    job = jobs.manager.submit(
        params,
        runner=tracked,
        # Namespaced by account so two customers requesting the same team do not collide.
        key=f"acct{account['id']}:{spec['dedup_key'](params)}",
        meta={
            'account_id': account['id'],
            'report_type': report_type,
            'subject': subject,
        },
    )
    usage.attach_job(usage_row, job['job_id'])
    if job.get('deduplicated'):
        # A build for this subject was already running, so `tracked` never runs and
        # would never close this row out. Still counts as a call, just not as work.
        usage.mark_complete(usage_row, 'duplicate')

    view = jobs.public_view(job)
    view['message'] = 'Report generation started. Poll /v1/reports/{job_id} for progress.'
    return jsonify(view), 202


def _owned_job(job_id: str):
    """Fetch a job, enforcing that it belongs to the calling account."""
    job = jobs.manager.get_by_id(job_id)
    if not job or job.get('account_id') != request.account['id']:
        # Do not distinguish "not yours" from "does not exist" — that would let a caller
        # enumerate other accounts' job ids.
        return None
    return job


@bp.route('/reports/<job_id>', methods=['GET'])
@require_account
def report_status(job_id):
    job = _owned_job(job_id)
    if not job:
        return _error('Job not found.', 404)
    view = jobs.public_view(job)
    filename = (job.get('result') or {}).get('filename')
    view['report_ready'] = bool(
        filename and os.path.exists(
            reports_store.resolve(request.account['id'], filename))
    )
    if view['report_ready']:
        view['download_url'] = f"/v1/reports/{job_id}/download"
    return jsonify(view), 200


@bp.route('/reports/<job_id>/download', methods=['GET'])
@require_account
def report_download(job_id):
    job = _owned_job(job_id)
    if not job:
        return _error('Job not found.', 404)
    if job['state'] != 'done':
        return _error(
            f"Report is not ready (state: {job['state']}).",
            409,
            state=job['state'],
            percent=job.get('percent'),
        )

    filename = (job.get('result') or {}).get('filename')
    try:
        path = reports_store.resolve(request.account['id'], filename) if filename else None
    except ValueError:
        return _error('Report file is not readable for this account.', 410)
    if not path or not os.path.exists(path):
        return _error('Report file is no longer on disk. Generate it again.', 410)

    return send_file(path, mimetype='application/pdf',
                     as_attachment=True, download_name=filename)


@bp.route('/reports', methods=['GET'])
@require_account
def list_reports():
    """Jobs this account has run during the current service lifetime."""
    mine = jobs.manager.for_account(request.account['id'])
    mine.sort(key=lambda j: j['created_at'], reverse=True)
    return jsonify({'reports': [jobs.public_view(j) for j in mine], 'count': len(mine)}), 200


# ---------------------------------------------------------------------------
# Account self-service
# ---------------------------------------------------------------------------
@bp.route('/account', methods=['GET'])
@require_account
def get_account():
    return jsonify(accounts.public_view(request.account)), 200


@bp.route('/account/usage', methods=['GET'])
@require_account
def account_usage():
    """This account's API call counts and recent request history."""
    account_id = request.account['id']
    summary = usage.summary_by_account().get(account_id) or {
        'total': 0, 'done': 0, 'error': 0, 'running': 0,
        'last_30d': 0, 'last_used': None, 'by_type': {},
    }
    last_used = summary.get('last_used')
    return jsonify({
        'account_id': account_id,
        'total_requests': summary['total'],
        'completed': summary['done'],
        'failed': summary['error'],
        'in_progress': summary['running'],
        'last_30_days': summary['last_30d'],
        'last_used': last_used.isoformat() if hasattr(last_used, 'isoformat') else last_used,
        'by_report_type': summary['by_type'],
        'recent': [
            {**r, 'created_at': (r['created_at'].isoformat()
                                 if hasattr(r['created_at'], 'isoformat') else r['created_at'])}
            for r in usage.recent(account_id, limit=25)
        ],
    }), 200


@bp.route('/account/settings', methods=['PATCH', 'POST'])
@require_account
def patch_settings():
    """Change models, search engine/depth, or reasoning effort.

    Only the supplied keys change. Send a key as null to drop the override and fall back
    to the service default.
    """
    data = _body()
    patch = data.get('settings') if isinstance(data.get('settings'), dict) else data
    patch = {k: v for k, v in (patch or {}).items() if k != 'api_key'}
    if not patch:
        return _error(
            'No settings supplied.',
            400,
            allowed_settings=list(config.ACCOUNT_SETTING_KEYS),
        )
    try:
        account = accounts.merge_settings(request.account['id'], patch)
    except accounts.AccountError as e:
        return _error(e.message, e.status, allowed_settings=list(config.ACCOUNT_SETTING_KEYS))
    return jsonify(accounts.public_view(account)), 200


@bp.route('/account/watermark', methods=['POST'])
@require_account
def upload_watermark():
    """Upload a watermark image.

    Accepts either multipart/form-data with a "file" (or "image") part, or JSON with a
    base64 "image_base64" field — whichever the client finds easier.
    """
    data, content_type = None, ''

    upload = request.files.get('file') or request.files.get('image')
    if upload is not None:
        data = upload.read()
        content_type = upload.mimetype or ''
    else:
        payload = request.get_json(silent=True) or {}
        raw = payload.get('image_base64') or payload.get('image')
        if raw:
            if isinstance(raw, str) and raw.startswith('data:'):
                header, _, encoded = raw.partition(',')
                content_type = header[5:].split(';')[0]
                raw = encoded
            try:
                data = base64.b64decode(raw, validate=True)
            except Exception:
                return _error('image_base64 is not valid base64.', 400)
            content_type = content_type or payload.get('content_type', '')

    if not data:
        return _error(
            'No image supplied. Send multipart/form-data with a "file" part, '
            'or JSON with "image_base64".',
            400,
        )
    if content_type and content_type.split(';')[0].strip().lower() not in config.ALLOWED_WATERMARK_TYPES:
        return _error(
            f"Unsupported image type '{content_type}'. "
            f"Allowed: {', '.join(config.ALLOWED_WATERMARK_TYPES)}",
            415,
        )

    try:
        saved = accounts.save_watermark(request.account['id'], data, content_type)
    except accounts.AccountError as e:
        return _error(e.message, e.status)

    return jsonify({
        'message': 'Watermark updated. It applies to every report generated from now on.',
        'bytes': saved['bytes'],
        'width': saved['width'],
        'height': saved['height'],
        'account': accounts.public_view(saved['account']),
    }), 200


@bp.route('/account/watermark', methods=['GET'])
@require_account
def get_watermark():
    path = accounts.watermark_path(request.account)
    custom = bool(request.account.get('watermark_file'))
    if request.args.get('download') in ('1', 'true', 'yes'):
        if not os.path.exists(path):
            return _error('No watermark image on disk.', 404)
        return send_file(path, mimetype='image/png')
    return jsonify({
        'has_custom_watermark': custom,
        'filename': request.account.get('watermark_file'),
        'using': 'account' if custom else 'service_default',
        'bytes': os.path.getsize(path) if os.path.exists(path) else 0,
    }), 200


@bp.route('/account/watermark', methods=['DELETE'])
@require_account
def delete_watermark():
    account = accounts.clear_watermark(request.account['id'])
    return jsonify({
        'message': 'Custom watermark removed; reports revert to the service default.',
        'account': accounts.public_view(account),
    }), 200


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------
@bp.route('/admin/accounts', methods=['POST'])
@require_admin
def create_account():
    """Create an account. The plaintext API key is returned ONCE and never stored."""
    data = _body()
    name = (data.get('account_name') or '').strip()
    if not name:
        return _error("'account_name' is required.", 400)

    allowed = data.get('allowed_reports')
    if allowed is None:
        allowed = ['matchup', 'team']
    if isinstance(allowed, str):
        allowed = [a.strip() for a in allowed.split(',') if a.strip()]
    unknown = [a for a in allowed if a not in report_types.REPORT_TYPES]
    if unknown:
        return _error(
            f"Unknown report type(s): {', '.join(unknown)}",
            400,
            available=sorted(report_types.REPORT_TYPES),
        )

    try:
        account, api_key = accounts.create(
            name,
            allowed,
            contact_email=data.get('contact_email'),
            settings=data.get('settings') or {},
            is_admin=bool(data.get('is_admin')),
        )
    except accounts.AccountError as e:
        return _error(e.message, e.status)

    return jsonify({
        'message': 'Account created. Store the api_key now — it cannot be retrieved again.',
        'api_key': api_key,
        'account': accounts.public_view(account),
    }), 201


@bp.route('/admin/accounts', methods=['GET'])
@require_admin
def list_accounts():
    rows = accounts.list_all()
    return jsonify({
        'accounts': [accounts.public_view(a, include_settings=False) for a in rows],
        'count': len(rows),
    }), 200


@bp.route('/admin/accounts/<int:account_id>', methods=['GET'])
@require_admin
def get_one_account(account_id):
    account = accounts.get(account_id)
    if not account:
        return _error('Account not found.', 404)
    return jsonify(accounts.public_view(account)), 200


@bp.route('/admin/accounts/<int:account_id>', methods=['PATCH'])
@require_admin
def update_account(account_id):
    """Change entitlements, activation, name, or settings."""
    if not accounts.get(account_id):
        return _error('Account not found.', 404)

    data = _body()
    fields = {}
    for key in ('account_name', 'contact_email', 'active', 'is_admin'):
        if key in data:
            fields[key] = data[key]
    if 'allowed_reports' in data:
        allowed = data['allowed_reports']
        if isinstance(allowed, str):
            allowed = [a.strip() for a in allowed.split(',') if a.strip()]
        unknown = [a for a in (allowed or []) if a not in report_types.REPORT_TYPES]
        if unknown:
            return _error(f"Unknown report type(s): {', '.join(unknown)}", 400)
        fields['allowed_reports'] = allowed
    if 'settings' in data:
        fields['settings'] = data['settings']

    if not fields:
        return _error(
            'Nothing to update.',
            400,
            updatable=['account_name', 'contact_email', 'active', 'is_admin',
                       'allowed_reports', 'settings'],
        )
    try:
        account = accounts.update(account_id, **fields)
    except accounts.AccountError as e:
        return _error(e.message, e.status)
    return jsonify(accounts.public_view(account)), 200


@bp.route('/admin/accounts/<int:account_id>/rotate-key', methods=['POST'])
@require_admin
def rotate_account_key(account_id):
    """Issue a new key. The previous key stops working immediately."""
    try:
        account, api_key = accounts.rotate_key(account_id)
    except accounts.AccountError as e:
        return _error(e.message, e.status)
    return jsonify({
        'message': 'Key rotated. The previous key is now invalid. Store this one — '
                   'it cannot be retrieved again.',
        'api_key': api_key,
        'account': accounts.public_view(account),
    }), 200


@bp.route('/admin/accounts/<int:account_id>', methods=['DELETE'])
@require_admin
def deactivate_account(account_id):
    """Deactivate rather than delete, so job history and audit trail survive."""
    if not accounts.get(account_id):
        return _error('Account not found.', 404)
    account = accounts.update(account_id, active=False)
    return jsonify({
        'message': 'Account deactivated. Its API key no longer authenticates.',
        'account': accounts.public_view(account, include_settings=False),
    }), 200
