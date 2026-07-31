import glob
import json
import logging
import os
import time
from datetime import datetime
from urllib.parse import urlsplit

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, request, send_file, jsonify
from werkzeug.exceptions import HTTPException

import cfbd
import config
import db
import jobs
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
# Scheduler: Rotowire scrape via Bright Data
# ---------------------------
sched = BackgroundScheduler(timezone="America/New_York")


@sched.scheduled_job(CronTrigger(hour=9, minute=0))
@sched.scheduled_job(CronTrigger(hour=18, minute=0))
def scheduled_rotowire_job():
    conn = None
    try:
        logging.info("Starting scheduled Rotowire scrape job...")
        bright_key = db.get_api_key('bright')
        if not bright_key:
            logging.error("Bright Data API key not found. Rotowire scrape aborted.")
            return

        # Trigger Bright Data collector for Rotowire
        collector_id = 'c_meewnv1y2gctpr239v'  # original
        trigger_url = f"https://api.brightdata.com/dca/trigger?queue_next=1&collector={collector_id}"
        headers = {"Authorization": f"Bearer {bright_key}", "Content-Type": "application/json"}
        trig = requests.post(trigger_url, json=[{}], headers=headers, timeout=30)
        if trig.status_code != 200:
            logging.error(f"Failed to trigger Rotowire scrape. Status: {trig.status_code}, Response: {trig.text}")
            return
        data = trig.json()
        collection_id = data.get('collection_id')
        if not collection_id:
            logging.error("No collection_id returned from Bright Data trigger.")
            return

        dataset_url = f"https://api.brightdata.com/dca/dataset?id={collection_id}"
        bright_headers = {"Authorization": f"Bearer {bright_key}"}

        deadline = time.time() + 720
        rotowire_data = None
        while time.time() < deadline:
            resp = requests.get(dataset_url, headers=bright_headers, timeout=15)
            if resp.status_code == 200 and resp.text.strip():
                try:
                    rotowire_data = resp.json()
                except ValueError:
                    lines = resp.text.strip().splitlines()
                    rotowire_data = [json.loads(line) for line in lines if line.strip()]
                if rotowire_data:
                    break
            time.sleep(1)
        if not rotowire_data:
            logging.error("Rotowire data not ready or empty.")
            return

        # Insert rows into local SQLite DB
        conn = db.get_rotowire_db_connection()
        inserted = 0
        cur = conn.cursor()
        for entry in rotowire_data:
            player_name = (entry.get('player_name') or '').strip()
            headline = (entry.get('headline') or '').strip()
            team_name = (entry.get('team_name') or '').strip()
            date_text = (entry.get('date_text') or '').strip()
            news_text = (entry.get('news_text') or '').strip()
            source_name = (entry.get('source_name') or '').strip()
            position = (entry.get('position') or '').strip()
            analysis_text = (entry.get('analysis_text') or '').strip()

            cur.execute(
                "SELECT 1 FROM rotowire WHERE player_name=? AND headline=? AND team_name=? "
                "AND date_text=? AND news_text=? AND source_name=? AND position=? AND analysis_text=? LIMIT 1",
                (player_name, headline, team_name, date_text, news_text, source_name, position, analysis_text)
            )
            if cur.fetchone():
                continue
            cur.execute(
                "INSERT INTO rotowire (player_name, headline, team_name, date_text, news_text, source_name, position, analysis_text) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (player_name, headline, team_name, date_text, news_text, source_name, position, analysis_text)
            )
            inserted += 1
        conn.commit()
        cur.close()
        logging.info(f"Rotowire scrape completed. Inserted {inserted} new records.")
    except Exception:
        logging.exception("Error during Rotowire scheduled job")
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


sched.start()


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
                resp = openrouter.chat(
                    or_key, model,
                    [{"role": "user", "content": "Reply with the single word: ok"}],
                    max_tokens=16, timeout=60, retries=0,
                )
                models[role] = {"model": model, "ok": True,
                                "reply": openrouter.extract_text(resp)[:40]}
            except Exception as e:
                out["ok"] = False
                models[role] = {"model": model, "ok": False, "error": str(e)[:400]}
        out["checks"]["openrouter"] = {
            "ok": all(m["ok"] for m in models.values()),
            "key_prefix": or_key[:10] + "...",
            "models": models,
        }

    # --- Local dependencies ----------------------------------------------
    try:
        conn = db.get_rotowire_db_connection()
        rows = conn.execute("SELECT COUNT(*) FROM rotowire").fetchone()[0]
        conn.close()
        out["checks"]["rotowire_db"] = {"ok": True, "rows": rows, "path": config.ROTOWIRE_DB_PATH}
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

    return jsonify(out), (200 if out["ok"] else 502)


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
