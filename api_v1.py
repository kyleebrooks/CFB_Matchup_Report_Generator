"""Multi-tenant REST API (/v1) for CFBReports.com and other consumers.

Entirely separate from the legacy AFPLNA endpoints: /generate-report, /report-status,
/get-report and /has-report keep authenticating with SERVICE_API_KEY and behave exactly
as before. Everything here authenticates with a per-account key issued by an admin.
"""

import base64
import functools
import logging
import os
import time
from datetime import datetime
from urllib.parse import quote

from flask import Blueprint, jsonify, request, send_file

import accounts
import config
import jobs
import report_types
import reports_store
import usage

bp = Blueprint('api_v1', __name__, url_prefix='/v1')

# {year or None: {'at': epoch, 'teams': [...]}} — see list_teams.
_TEAMS_CACHE: dict = {}


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

    tier = (str(data.get('tier') or 'standard')).strip().lower()
    if tier not in ('standard', 'premium'):
        return _error("'tier' must be 'standard' or 'premium'.", 400)

    # Per-account model/search choices, watermark and output directory travel with the
    # job. The directory is what keeps two customers' PDFs apart: filenames are built
    # from subject and date, so without it the same matchup on the same day collides.
    params['settings'] = accounts.effective_settings(account)
    if tier == 'premium':
        # Premium swaps ONLY the synthesis model; the research/search model is
        # unchanged. The override rides inside the job's settings, so every report
        # pipeline gets it without knowing tiers exist.
        params['settings'] = dict(params['settings'],
                                  report_model=params['settings']['premium_report_model'])
    params['tier'] = tier
    params['watermark'] = accounts.watermark_path(account)
    params['report_dir'] = reports_store.account_dir(account['id'])
    params['account_id'] = account['id']

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
        # Namespaced by account so two customers requesting the same team do not
        # collide — and by tier, so a premium request is never deduplicated into a
        # standard build that happens to be running.
        key=f"acct{account['id']}:{tier}:{spec['dedup_key'](params)}",
        meta={
            'account_id': account['id'],
            'report_type': report_type,
            'tier': tier,
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


@bp.route('/reports/stored', methods=['GET'])
@require_account
def list_stored_reports():
    """Every PDF still on disk for this account, newest first.

    /v1/reports only knows about the current service lifetime, because the job table is
    in memory. This reads the account's own directory instead, so a consumer can list
    what actually exists after a restart — which is what a website needs to build a
    catalogue of published reports.
    """
    rows = reports_store.list_reports(request.account['id'])
    return jsonify({
        'reports': [{
            'filename': r['filename'],
            'report_type': r['report_type'],
            'subject': r['subject'],
            'game_date': r.get('game_date'),
            'bytes': r['bytes'],
            'generated_at': r['modified'].isoformat() if r['modified'] else None,
            'url': f"/v1/reports/stored/{quote(r['filename'])}",
        } for r in rows],
        'count': len(rows),
    }), 200


@bp.route('/reports/stored/<path:filename>', methods=['GET'])
@require_account
def get_stored_report(filename):
    """Fetch one stored PDF by name. Scoped to the caller's own directory."""
    try:
        path = reports_store.resolve(request.account['id'], filename)
    except ValueError:
        return _error('Invalid report filename.', 400)
    if not os.path.isfile(path):
        return _error('No such report for this account.', 404)
    # inline, not attachment: a consumer rendering this in a viewer should not be
    # handed a Content-Disposition that pushes the browser toward saving it.
    return send_file(path, mimetype='application/pdf', as_attachment=False,
                     download_name=os.path.basename(path))


@bp.route('/reports/stored/<path:filename>', methods=['DELETE'])
@require_account
def delete_stored_report(filename):
    try:
        result = reports_store.delete(request.account['id'], filename)
    except ValueError:
        return _error('Invalid report filename.', 400)
    if not result['ok']:
        return _error(result['error'], 404)
    return jsonify({'deleted': filename, 'bytes': result['bytes']}), 200


@bp.route('/teams', methods=['GET'])
@require_account
def list_teams():
    """Teams for the season, for populating a client's team pickers.

    FBS by default; `?include=fcs` appends the FCS division too, so a client can
    offer the opponents FBS schools actually schedule. Cached for an hour: rosters
    change a handful of times a year, and every consumer would otherwise hit CFBD
    on every page load.
    """
    global _TEAMS_CACHE
    year = request.args.get('year', type=int)
    include_fcs = (request.args.get('include') or '').strip().lower() == 'fcs'
    cache_key = (year, include_fcs)
    now = time.time()
    cached = _TEAMS_CACHE.get(cache_key)
    if cached and now - cached['at'] < 3600:
        return jsonify({'teams': cached['teams'], 'count': len(cached['teams']),
                        'cached': True}), 200

    import cfbd
    import db as db_mod

    api_key = db_mod.resolve_cfbd_key()
    if not api_key:
        return _error('No CollegeFootballData key configured on the service.', 503)
    errors: list[dict] = []
    season = year or cfbd.season_year(datetime.now())
    rows = cfbd._get(api_key, '/teams/fbs', {'year': season}, 'FBS teams', errors)
    if errors:
        return _error('CollegeFootballData request failed.', 502,
                      detail=f"HTTP {errors[0]['status']}: {errors[0]['body'][:200]}")

    def shape(row, classification):
        school = (row.get('school') or '').strip()
        if not school:
            return None
        mascot = (row.get('mascot') or '').strip()
        return {
            'school': school,
            'mascot': mascot,
            'full_name': f'{school} {mascot}'.strip(),
            'conference': (row.get('conference') or '').strip(),
            'classification': classification,
        }

    teams = [t for t in (shape(r, 'fbs') for r in rows or []) if t]
    if include_fcs:
        # The full roster is a fail-soft extra: a hiccup here still returns FBS.
        try:
            fcs_rows = [r for r in cfbd.all_teams(api_key, season, errors)
                        if (r.get('classification') or '').lower() == 'fcs']
            seen = {t['school'] for t in teams}
            teams += [t for t in (shape(r, 'fcs') for r in fcs_rows)
                      if t and t['school'] not in seen]
        except Exception as e:
            logging.warning(f'FCS roster unavailable; returning FBS only: {e}')
    teams.sort(key=lambda t: t['school'].lower())
    _TEAMS_CACHE[cache_key] = {'at': now, 'teams': teams}
    return jsonify({'teams': teams, 'count': len(teams), 'cached': False}), 200


_GAMES_CACHE: dict = {}


@bp.route('/games', methods=['GET'])
@require_account
def list_games():
    """The games a client can offer in a picker.

    Without parameters: two windows derived from the CFBD calendar around today —
    `upcoming` (this week and next, for the matchup selector) and `recent` (the last
    two weeks' finals, for the Full Game Recap selector). With `year` (and optionally
    `week` and `season_type`): that explicit week, split the same way — which is how
    a client reaches prior seasons' finals and future weeks' matchups. Both shapes
    include the season's `weeks` list and the `current` week, so a client can build
    its year/week selectors from the response alone. Cached for ten minutes per
    distinct selection.
    """
    global _GAMES_CACHE
    year = request.args.get('year', type=int)
    week = request.args.get('week', type=int)
    season_type = (request.args.get('season_type') or '').strip() or None
    if year is not None and not 1900 <= year <= 2200:
        return _error("'year' must be a four-digit season, e.g. 2025", 400)
    if week is not None and not 1 <= week <= 30:
        return _error("'week' must be between 1 and 30", 400)
    if season_type and season_type not in ('regular', 'postseason'):
        return _error("'season_type' must be 'regular' or 'postseason'", 400)

    cache_key = (year, week, season_type)
    now = time.time()
    cached = _GAMES_CACHE.get(cache_key)
    if cached and now - cached['at'] < 600:
        payload = dict(cached['data'])
        payload['cached'] = True
        return jsonify(payload), 200

    import cfbd
    import db as db_mod

    api_key = db_mod.resolve_cfbd_key()
    if not api_key:
        return _error('No CollegeFootballData key configured on the service.', 503)

    if year is None and week is None and season_type is None:
        windows = cfbd.schedule_windows(api_key)
    else:
        from datetime import datetime, timezone
        resolved_year = year or cfbd.season_year(datetime.now(timezone.utc))
        windows = cfbd.week_games(api_key, resolved_year, week, season_type)

    if not windows['upcoming'] and not windows['recent'] and windows['errors']:
        first = windows['errors'][0]
        return _error('CollegeFootballData request failed.', 502,
                      detail=f"HTTP {first['status']}: {str(first['body'])[:200]}")

    payload = {
        'season': windows['season'],
        'weeks': windows['weeks'],
        'current': windows['current'],
        'selected': windows['selected'],
        'upcoming': windows['upcoming'],
        'recent': windows['recent'],
        'count': len(windows['upcoming']) + len(windows['recent']),
        'cached': False,
    }
    _GAMES_CACHE[cache_key] = {'at': now, 'data': payload}
    while len(_GAMES_CACHE) > 64:
        _GAMES_CACHE.pop(next(iter(_GAMES_CACHE)))
    return jsonify(payload), 200


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


@bp.route('/podcasts', methods=['POST'])
@require_account
def create_podcast():
    """Queue a podcast episode: paste a script, name a TTS model, poll the job.

    Returns 202 with the same job handle shape as reports — poll
    GET /v1/reports/{job_id} until done; the result carries the episode filename.
    """
    import podcasts
    account = request.account
    data = _body()

    script = (data.get('script') or '').strip()
    if not script:
        return _error("'script' is required — paste the episode text.", 400)
    if len(script) > podcasts.MAX_SCRIPT_CHARS:
        return _error(f'Script is too long ({len(script)} chars; the limit is '
                      f'{podcasts.MAX_SCRIPT_CHARS}).', 413)
    tts_model = (data.get('tts_model') or '').strip()
    if '/' not in tts_model:
        return _error("'tts_model' must be a full OpenRouter model id, e.g. "
                      "'fish-audio/s2.1-pro'.", 400)
    voice = (data.get('voice') or '').strip() or None
    title = (data.get('title') or '').strip() or None
    clone_audio = (data.get('clone_audio') or '').strip() or None
    clone_transcript = (data.get('clone_transcript') or '').strip() or None
    try:
        podcasts.validate_clone(clone_audio)
    except podcasts.PodcastError as e:
        return _error(str(e), e.status)

    params = {
        'script': script, 'tts_model': tts_model, 'voice': voice, 'title': title,
        'clone_audio': clone_audio, 'clone_transcript': clone_transcript,
        'account_id': account['id'],
    }

    def run(job_params, progress):
        return podcasts.generate(
            script=job_params['script'], tts_model=job_params['tts_model'],
            voice=job_params.get('voice'),
            clone_audio=job_params.get('clone_audio'),
            clone_transcript=job_params.get('clone_transcript'),
            title=job_params.get('title'),
            account_id=job_params['account_id'], progress=progress)

    usage_row = usage.record_request(account['id'], 'podcast',
                                     title or 'podcast episode', '')

    def tracked(job_params, progress, _row=usage_row):
        try:
            result = run(job_params, progress)
        except Exception as e:
            usage.mark_complete(_row, 'error', error=f'{e.__class__.__name__}: {e}')
            raise
        usage.mark_complete(_row, 'done')
        return result

    import hashlib
    # Model, voice and clone sample are part of the episode's identity: asking
    # again with a different voice is a new episode, not a duplicate request.
    fingerprint = '\x00'.join([script, tts_model, voice or '',
                               (clone_audio or '')[:80]])
    digest = hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:12]
    job = jobs.manager.submit(
        params,
        runner=tracked,
        key=f"acct{account['id']}:podcast:{digest}",
        meta={'account_id': account['id'], 'report_type': 'podcast',
              'subject': title or 'podcast episode'},
    )
    status = 200 if job.get('deduplicated') else 202
    return jsonify({'job_id': job['job_id'], 'state': job['state'],
                    'message': job.get('message'),
                    'deduplicated': bool(job.get('deduplicated'))}), status


@bp.route('/feeds', methods=['GET'])
@require_account
def feed_settings():
    """Both feeds' scheduler settings and last-run state."""
    import feeds
    return jsonify({'feeds': feeds.get_settings()}), 200


@bp.route('/feeds/<feed>/settings', methods=['PUT'])
@require_account
def update_feed_settings(feed):
    """Console-editable scheduler settings: on/off, interval, model, engine."""
    import feeds
    data = _body()
    try:
        row = feeds.update_settings(feed, data)
    except feeds.FeedError as e:
        return _error(str(e), e.status)
    return jsonify(row), 200


@bp.route('/feeds/<feed>/items', methods=['GET'])
@require_account
def feed_items(feed):
    import feeds
    try:
        rows = feeds.items(feed,
                           limit=request.args.get('limit', 25),
                           before_id=request.args.get('before'))
    except feeds.FeedError as e:
        return _error(str(e), e.status)
    except (TypeError, ValueError):
        return _error("'limit' and 'before' must be integers.", 400)
    return jsonify({'feed': feed, 'items': rows, 'count': len(rows)}), 200


@bp.route('/feeds/<feed>/pull', methods=['POST'])
@require_account
def pull_feed(feed):
    """Kick a pull now, off-schedule. Runs in the background — research takes
    the better part of a minute; poll GET /v1/feeds for the outcome."""
    import feeds
    if feed not in feeds.FEEDS:
        return _error(f"Unknown feed '{feed}'. Feeds: {', '.join(feeds.FEEDS)}", 400)

    def run():
        try:
            feeds.run_pull(feed)
        except Exception:
            import logging
            logging.exception(f"Manual feed pull '{feed}' failed")

    import threading
    threading.Thread(target=run, daemon=True, name=f'feed-pull-{feed}').start()
    return jsonify({'feed': feed, 'started': True}), 202


@bp.route('/podcasts/upload', methods=['POST'])
@require_account
def upload_podcast():
    """Catalogue a finished episode produced elsewhere.

    The request body is the raw audio file (MP3 or WAV — verified by content,
    not extension); the optional title travels as a query parameter. Streamed
    to disk, so the file's size never sits in this process's memory.
    """
    import podcasts
    account = request.account
    title = (request.args.get('title') or '').strip() or None
    try:
        result = podcasts.store_upload(stream=request.stream,
                                       account_id=account['id'], title=title)
    except podcasts.PodcastError as e:
        return _error(str(e), e.status)
    usage_row = usage.record_request(account['id'], 'podcast_upload',
                                     result['title'], '')
    usage.mark_complete(usage_row, 'done')
    return jsonify(result), 201


@bp.route('/podcasts', methods=['GET'])
@require_account
def list_podcasts():
    import podcasts
    try:
        episodes = podcasts.list_for(request.account['id'])
    except Exception as e:
        return _error('Podcast store unavailable.', 503, detail=str(e)[:200])
    return jsonify({'podcast_episodes': episodes, 'count': len(episodes)}), 200


@bp.route('/podcasts/audio/<path:filename>', methods=['GET'])
@require_account
def podcast_audio(filename):
    """The episode audio itself, with conditional/Range support for streaming."""
    import podcasts
    from flask import send_file
    try:
        path = podcasts.resolve(request.account['id'], filename)
    except podcasts.PodcastError as e:
        return _error(str(e), e.status)
    if not os.path.isfile(path):
        return _error('No such episode.', 404)
    return send_file(path, mimetype='audio/mpeg', conditional=True)


@bp.route('/podcasts/<path:filename>', methods=['DELETE'])
@require_account
def delete_podcast(filename):
    import podcasts
    try:
        return jsonify(podcasts.delete(request.account['id'], filename)), 200
    except podcasts.PodcastError as e:
        return _error(str(e), e.status)


@bp.route('/account/content/<key>', methods=['GET'])
@require_account
def get_site_content(key):
    """One stored content entry (e.g. the site's About page copy)."""
    import content_store
    try:
        return jsonify(content_store.get(request.account['id'], key)), 200
    except content_store.ContentError as e:
        return _error(str(e), e.status)
    except Exception as e:
        return _error('Content store unavailable.', 503, detail=str(e)[:200])


@bp.route('/account/content/<key>', methods=['PUT', 'POST'])
@require_account
def put_site_content(key):
    """Replace one content entry. Body: {"content": "..."}."""
    import content_store
    data = _body()
    if 'content' not in data:
        return _error("'content' is required.", 400)
    try:
        saved = content_store.put(request.account['id'], key, data.get('content'))
    except content_store.ContentError as e:
        return _error(str(e), e.status)
    except Exception as e:
        return _error('Content store unavailable.', 503, detail=str(e)[:200])
    return jsonify(saved), 200


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
# Recurring report schedules (per account, optional)
# ---------------------------------------------------------------------------
def _schedule_view(row: dict) -> dict:
    import schedules as schedules_mod
    out = {k: row.get(k) for k in ('id', 'report_type', 'scope', 'day_of_week',
                                   'hour_utc', 'max_reports', 'last_result')}
    out['enabled'] = bool(row.get('enabled'))
    out['day_name'] = schedules_mod.DAYS[int(row['day_of_week']) % 7]
    for key in ('last_run_at', 'created_at'):
        v = row.get(key)
        out[key] = v.isoformat() if hasattr(v, 'isoformat') else v
    return out


@bp.route('/account/schedules', methods=['GET'])
@require_account
def list_schedules():
    import schedules as schedules_mod
    mine = [r for r in schedules_mod.list_all()
            if r['account_id'] == request.account['id']]
    return jsonify({'schedules': [_schedule_view(r) for r in mine],
                    'valid_report_types': list(schedules_mod.VALID_TYPES)}), 200


@bp.route('/account/schedules', methods=['POST'])
@require_account
def create_schedule():
    """Create a weekly recurring report. Times are UTC; day 0 = Monday."""
    import schedules as schedules_mod
    data = _body()
    rtype = (data.get('report_type') or '').strip().lower()
    if rtype and rtype not in (request.account.get('allowed_reports') or []):
        return _error(f"This account is not entitled to '{rtype}'.", 403)
    try:
        row = schedules_mod.create(
            account_id=request.account['id'],
            report_type=rtype,
            scope=data.get('scope') or 'top25',
            day_of_week=int(data.get('day_of_week', 1)),
            hour_utc=int(data.get('hour_utc', 12)),
            max_reports=int(data.get('max_reports', 10)),
        )
    except (schedules_mod.ScheduleError, TypeError, ValueError) as e:
        return _error(str(e), 400)
    return jsonify({'schedule': _schedule_view(row)}), 201


def _owned_schedule(schedule_id: int):
    import schedules as schedules_mod
    row = schedules_mod.get(schedule_id)
    if not row or row['account_id'] != request.account['id']:
        return None
    return row


@bp.route('/account/schedules/<int:schedule_id>', methods=['PATCH'])
@require_account
def update_schedule(schedule_id):
    """Adjust a schedule: enabled on/off, scope, day, hour, or report cap."""
    import schedules as schedules_mod
    row = _owned_schedule(schedule_id)
    if not row:
        return _error('No such schedule.', 404)
    data = _body()
    editable = ('enabled', 'scope', 'day_of_week', 'hour_utc', 'max_reports')
    if not any(k in data for k in editable):
        return _error(f"Send at least one of: {', '.join(editable)}.", 400)
    try:
        if any(k in data for k in editable[1:]):
            schedules_mod.update(
                schedule_id,
                scope=data.get('scope'),
                day_of_week=data.get('day_of_week'),
                hour_utc=data.get('hour_utc'),
                max_reports=data.get('max_reports'),
            )
        if 'enabled' in data:
            schedules_mod.set_enabled(schedule_id, bool(data['enabled']))
    except (schedules_mod.ScheduleError, TypeError, ValueError) as e:
        return _error(str(e), 400)
    return jsonify({'schedule': _schedule_view(schedules_mod.get(schedule_id))}), 200


@bp.route('/account/schedules/<int:schedule_id>', methods=['DELETE'])
@require_account
def delete_schedule(schedule_id):
    import schedules as schedules_mod
    row = _owned_schedule(schedule_id)
    if not row:
        return _error('No such schedule.', 404)
    schedules_mod.delete(schedule_id)
    return jsonify({'message': 'Schedule deleted.'}), 200


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
