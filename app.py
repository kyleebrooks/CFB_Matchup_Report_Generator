import glob
import logging
import os
import time
from datetime import datetime
from urllib.parse import urlsplit

from flask import Flask, request, send_file, jsonify
from werkzeug.exceptions import HTTPException

import api_v1
import cfbd
import config
import db
import jobs
import injuries
import pipeline

# ---------------------------
# App & CORS
# ---------------------------
app = Flask(__name__)

# --- ONE CORS LAYER ONLY ---
ALLOWED_ORIGINS = {
    "http://afplnapicks.com",
    "http://www.afplnapicks.com",
    "https://afplnapicks.com",
    "https://www.afplnapicks.com",
    "http://www.afplnapicks.com/PicksSite/",
    "http://afplnapicks.com/PicksSite/",
    "https://www.afplnapicks.com/PicksSite/",
    "https://afplnapicks.com/PicksSite/",
}
ALLOWED = {f"{urlsplit(o).scheme}://{urlsplit(o).hostname}".lower() for o in ALLOWED_ORIGINS}


def _set_cors_headers(resp, origin_hdr):
    # Normalize the Origin (ignore port)
    allowed = None
    if origin_hdr:
        o = urlsplit(origin_hdr)
        norm = f"{o.scheme}://{o.hostname}".lower()
        allowed = norm in ALLOWED
        if allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin_hdr   # echo exact
            resp.headers["Access-Control-Allow-Credentials"] = "true"
        else:
            # Fallback so GET/HEAD JSON is still readable even if scheme/domain mismatch
            if request.method in ("GET", "HEAD"):
                resp.headers["Access-Control-Allow-Origin"] = "*"
    else:
        # No Origin header (e.g., curl) — allow reads
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")

    # Be explicit
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    vary = resp.headers.get("Vary")
    resp.headers["Vary"] = (vary + ", Origin") if vary else "Origin"
    return resp


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        resp = app.make_response(("", 204))
        return _set_cors_headers(resp, request.headers.get("Origin"))


@app.after_request
def add_cors(resp):
    return _set_cors_headers(resp, request.headers.get("Origin"))


logging.basicConfig(level=logging.INFO)

os.makedirs(config.REPORTS_DIR, exist_ok=True)
os.makedirs(config.WATERMARKS_DIR, exist_ok=True)

# Multi-tenant API for CFBReports.com and other consumers. Mounted under /v1 so the
# legacy AFPLNA endpoints below keep their exact URLs, auth and behaviour.
app.register_blueprint(api_v1.bp)


# ---------------------------
# Helpers
# ---------------------------
def _extract_api_key():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    x = request.headers.get("X-Api-Key")
    if x:
        return x.strip()
    # Fallbacks for older clients:
    body = request.get_json(silent=True) or {}
    return request.args.get("api_key") or body.get("api_key") or request.form.get("api_key")


# JSON error handler for easier debugging
@app.errorhandler(Exception)
def handle_any_error(e):
    if isinstance(e, HTTPException):
        return e
    logging.exception("Unhandled error")
    return jsonify({
        "error": "Server error",
        "type": e.__class__.__name__,
        "detail": str(e),
    }), 500


# ---------------------------
# Injury feed
# ---------------------------
# There is no scheduled job any more. The Bright Data scrape it used to run is gone —
# Rotowire blocks automated clients, which is the only reason that proxy was ever in the
# stack. The feed is now collected on demand: every report already makes a per-team
# injury research call, and injuries.record_findings() persists what it found, so the
# table fills as a side effect of normal use. A team nobody has reported on recently is
# refreshed by injuries.ensure_fresh() when a report asks for it and the cached rows are
# older than INJURY_FEED_TTL_HOURS.
#
# A deliberate sweep of every FBS team is still available, but only as an explicit act:
#     admin_tui.py injuries --sweep


# ---------------------------
# Routes
# ---------------------------
@app.route('/ping')
def ping():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat() + "Z"})


@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "CFB Matchup Report Generator running"}), 200


@app.route('/generate-report', methods=['POST'])
def generate_report():
    """Queue a report build and return immediately.

    Generation takes several minutes, which is far longer than a browser or proxy will
    hold a connection open. The client gets 202 plus a job handle and polls
    /report-status. Pass wait=true to block until the report is finished instead (handy
    for curl and cron; the caller owns the timeout).
    """
    if request.content_type and request.content_type.startswith("application/x-www-form-urlencoded"):
        data = request.form.to_dict(flat=True)
    else:
        data = request.get_json(force=True, silent=True) or {}

    user_api_key = _extract_api_key()
    if config.SERVICE_API_KEY and user_api_key != config.SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    home_full = data.get('home_full')
    away_full = data.get('away_full')
    home_short = data.get('home_short')
    away_short = data.get('away_short')
    if not all([home_full, away_full, home_short, away_short]):
        return jsonify({"error": "Missing team name parameters"}), 400

    try:
        year = int(data.get('year')) if data.get('year') else None
    except (TypeError, ValueError):
        year = None

    params = {
        "home_full": home_full,
        "away_full": away_full,
        "home_short": home_short,
        "away_short": away_short,
        "year": year,
        "kickoff": data.get('kickoff'),
    }

    wait = str(data.get('wait') or request.args.get('wait') or '').lower() in ('1', 'true', 'yes')
    if wait:
        try:
            result = pipeline.generate(**params)
        except pipeline.PipelineError as e:
            return jsonify({"error": e.message, "detail": e.detail}), e.status
        return jsonify({"message": "Report generated successfully", **result}), 200

    job = jobs.manager.submit(params)
    view = jobs.public_view(job)
    view["message"] = "Report generation started. Poll /report-status for progress."
    return jsonify(view), 202


@app.route('/report-status', methods=['GET'])
def report_status():
    """Progress for the most recent build of a matchup.

    state is one of: queued, running, done, error, or none when nothing has been
    queued this process lifetime. `report_exists` reflects what is actually on disk,
    so the frontend can offer a download even after a restart cleared the job table.
    """
    api_key_param = _extract_api_key()
    if config.SERVICE_API_KEY and api_key_param != config.SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    home_short = request.args.get('home_team')
    away_short = request.args.get('away_team')
    if not home_short or not away_short:
        return jsonify({"error": "Missing team name parameters"}), 400

    pattern = os.path.join(config.REPORTS_DIR, f"{home_short}_{away_short}_*.pdf")
    exists = bool(glob.glob(pattern))

    job = jobs.manager.get(home_short, away_short)
    if not job:
        return jsonify({
            "state": "none",
            "stage": "none",
            "message": "Report is ready" if exists else "No report generated yet",
            "percent": 100 if exists else 0,
            "report_exists": exists,
            "home_team": home_short,
            "away_team": away_short,
        }), 200

    view = jobs.public_view(job)
    view["report_exists"] = exists
    return jsonify(view), 200


@app.route('/get-report', methods=['GET'])
def get_report():
    api_key_param = _extract_api_key()
    if config.SERVICE_API_KEY and api_key_param != config.SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    home_short = request.args.get('home_team')
    away_short = request.args.get('away_team')
    if not home_short or not away_short:
        return jsonify({"error": "Missing team name parameters"}), 400

    pattern = os.path.join(config.REPORTS_DIR, f"{home_short}_{away_short}_*.pdf")
    files = glob.glob(pattern)
    if not files:
        return jsonify({"error": "Report not found. Please generate it first."}), 404

    filepath = max(files, key=os.path.getmtime)
    filename = os.path.basename(filepath)

    return send_file(filepath, mimetype='application/pdf', as_attachment=True, download_name=filename)


@app.route('/has-report', methods=['GET'])
def has_report():
    api_key_param = _extract_api_key()
    if config.SERVICE_API_KEY and api_key_param != config.SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    home_short = request.args.get('home_team')
    away_short = request.args.get('away_team')
    if not home_short or not away_short:
        return jsonify({"error": "Missing team name parameters"}), 400

    pattern = os.path.join(config.REPORTS_DIR, f"{home_short}_{away_short}_*.pdf")
    files = glob.glob(pattern)
    logging.info(f"[has-report] REPORTS_DIR={config.REPORTS_DIR} pattern={pattern} matches={len(files)}")
    return jsonify({"exists": bool(files)}), 200


@app.route('/health', methods=['GET'])
def health():
    """One call that checks every external dependency. Start here when a report fails."""
    api_key_param = _extract_api_key()
    if config.SERVICE_API_KEY and api_key_param != config.SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    out = {"ok": True, "checks": {}}
    year = cfbd.season_year(datetime.now())
    out["season_year"] = year

    # --- CFBD ------------------------------------------------------------
    cfbd_key = db.resolve_cfbd_key()
    if not cfbd_key:
        out["ok"] = False
        out["checks"]["cfbd"] = {
            "ok": False,
            "error": "No CFBD key found",
            "hint": "Add a 'CFD' (or 'CFBD') row to API_KEYS, or set CFBD_API_KEY.",
        }
    else:
        check = cfbd.check_key(cfbd_key, year)
        check["key_prefix"] = cfbd_key[:6] + "..."
        if not check["ok"]:
            out["ok"] = False
            check["hint"] = (
                "CFBD rejected the key. Confirm it at collegefootballdata.com/key and "
                "update the 'CFD' row in API_KEYS."
            )
        out["checks"]["cfbd"] = check

    # --- OpenRouter ------------------------------------------------------
    import openrouter

    or_key = db.resolve_openrouter_key()
    if not or_key:
        out["ok"] = False
        out["checks"]["openrouter"] = {
            "ok": False,
            "error": "No OpenRouter key found",
            "hint": "Add an 'openrouter' row to API_KEYS, or set OPENROUTER_API_KEY.",
        }
    else:
        models = {}
        for role, model in (("research", config.OPENROUTER_RESEARCH_MODEL),
                            ("report", config.OPENROUTER_REPORT_MODEL)):
            try:
                # 2048, not 16: a reasoning model spends tokens thinking before it
                # writes anything, so a tiny cap yields an empty reply and a false alarm.
                resp = openrouter.chat(
                    or_key, model,
                    [{"role": "user", "content": "Reply with the single word: ok"}],
                    max_tokens=2048, timeout=90, retries=0,
                )
                reply = openrouter.extract_text(resp)
                usage = openrouter.extract_usage(resp)
                entry = {
                    "model": model,
                    "ok": bool(reply),
                    "reply": reply[:40],
                    "finish_reason": openrouter.finish_reason(resp),
                    "reasoning_tokens": usage.get("reasoning_tokens"),
                }
                if not reply:
                    out["ok"] = False
                    entry["error"] = (
                        "Model returned no text. If finish_reason is 'length' the token "
                        "budget went entirely to reasoning — raise REPORT_MAX_TOKENS."
                    )
                models[role] = entry
            except Exception as e:
                out["ok"] = False
                models[role] = {"model": model, "ok": False, "error": str(e)[:400]}
        out["checks"]["openrouter"] = {
            "ok": all(m["ok"] for m in models.values()),
            "key_prefix": or_key[:10] + "...",
            "models": models,
        }

    # --- Local dependencies ----------------------------------------------
    # A row count alone called this healthy while the newest row aged past a year.
    # Freshness is the only thing that distinguishes a working feed from a dead one.
    try:
        st = injuries.status()
        stale = st["days_stale"]
        fresh = stale is not None and stale <= config.ROTOWIRE_STALE_DAYS
        check = {
            "ok": not st.get("error"),
            "fresh": fresh,
            "rows": st["rows"],
            "newest_row": st["newest_text"],
            "days_stale": stale,
            "path": st["path"],
        }
        if st.get("error"):
            check["error"] = st["error"][:300]
        elif not fresh:
            out["ok"] = False
            check["error"] = (
                f"The newest Rotowire row is {stale if stale is not None else 'un-dated'} "
                f"days old; the scrape runs twice a day. Run "
                f"'admin_tui.py injuries' on the droplet for the cause."
            )
        cov = injuries.coverage(0)
        check["teams_collected"] = cov.get("teams", 0)
        check["teams_fresh"] = cov.get("fresh", 0)
        out["checks"]["rotowire_db"] = check
    except Exception as e:
        out["checks"]["rotowire_db"] = {"ok": False, "error": str(e)[:300]}

    wk = config.WKHTMLTOPDF_PATH or "/usr/bin/wkhtmltopdf"
    out["checks"]["wkhtmltopdf"] = {"ok": os.path.exists(wk), "path": wk}
    if not out["checks"]["wkhtmltopdf"]["ok"]:
        out["ok"] = False

    out["checks"]["reports_dir"] = {
        "ok": os.path.isdir(config.REPORTS_DIR) and os.access(config.REPORTS_DIR, os.W_OK),
        "path": config.REPORTS_DIR,
    }
    out["checks"]["watermark"] = {"ok": os.path.exists(config.WATERMARK_PATH)}

    try:
        import accounts
        accounts.ensure_schema()
        rows = accounts.list_all()
        out["checks"]["accounts"] = {
            "ok": True,
            "count": len(rows),
            "active": sum(1 for a in rows if a["active"]),
            "admins": sum(1 for a in rows if a["is_admin"]),
            "bootstrap_admin_key_set": bool(config.ADMIN_API_KEY),
        }
    except Exception as e:
        out["ok"] = False
        out["checks"]["accounts"] = {"ok": False, "error": str(e)[:300]}

    out["checks"]["watermarks_dir"] = {
        "ok": os.path.isdir(config.WATERMARKS_DIR) and os.access(config.WATERMARKS_DIR, os.W_OK),
        "path": config.WATERMARKS_DIR,
    }

    import charts as charts_mod
    out["checks"]["charts"] = {
        "ok": charts_mod.CHARTS_AVAILABLE,
        "mplconfigdir": os.environ.get("MPLCONFIGDIR"),
    }
    if not charts_mod.CHARTS_AVAILABLE:
        out["ok"] = False
        out["checks"]["charts"]["error"] = charts_mod.IMPORT_ERROR
        out["checks"]["charts"]["hint"] = "pip install -r requirements.txt (matplotlib)"

    return jsonify(out), (200 if out["ok"] else 502)


@app.route('/health/cfbd', methods=['GET'])
def health_cfbd():
    """Probe every CFBD endpoint the report uses, one at a time.

    Run this when reports fail with empty statistics. Because it is sequential it
    distinguishes a key/tier rejection (that endpoint always 401/403s) from a rate
    limit (fine here, fails under the report's concurrent burst).
    """
    api_key_param = _extract_api_key()
    if config.SERVICE_API_KEY and api_key_param != config.SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    key = db.resolve_cfbd_key()
    if not key:
        return jsonify({
            "ok": False,
            "error": "No CFBD key found",
            "hint": "Add a 'CFD' (or 'CFBD') row to API_KEYS, or set CFBD_API_KEY.",
        }), 500

    try:
        year = int(request.args.get('year'))
    except (TypeError, ValueError):
        year = cfbd.season_year(datetime.now())

    result = cfbd.probe(key, year, request.args.get('team') or 'Georgia')
    result["key_prefix"] = key[:6] + "..."
    result["key_length"] = len(key)
    return jsonify(result), (200 if result["ok"] else 502)


@app.route('/health/llm', methods=['GET'])
def health_llm():
    """Confirm the OpenRouter key resolves and both models answer. Cheap smoke test."""
    api_key_param = _extract_api_key()
    if config.SERVICE_API_KEY and api_key_param != config.SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    import openrouter

    key = db.resolve_openrouter_key()
    if not key:
        return jsonify({
            "ok": False,
            "error": "No OpenRouter key found",
            "hint": "Add an 'openrouter' row to API_KEYS, or set OPENROUTER_API_KEY.",
        }), 500

    out = {"ok": True, "key_source": "resolved", "models": {}}
    for role, model in (("research", config.OPENROUTER_RESEARCH_MODEL),
                        ("report", config.OPENROUTER_REPORT_MODEL)):
        try:
            resp = openrouter.chat(
                key, model,
                [{"role": "user", "content": "Reply with the single word: ok"}],
                max_tokens=16, timeout=60, retries=0,
            )
            out["models"][role] = {
                "model": model,
                "ok": True,
                "reply": openrouter.extract_text(resp)[:40],
            }
        except Exception as e:
            out["ok"] = False
            out["models"][role] = {"model": model, "ok": False, "error": str(e)[:400]}
    return jsonify(out), (200 if out["ok"] else 502)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
