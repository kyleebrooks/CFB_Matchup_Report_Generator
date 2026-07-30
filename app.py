import glob
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, request, send_file, jsonify
from werkzeug.exceptions import HTTPException

import cfbd
import charts as charts_mod
import config
import db
import predict
import render
import report as report_mod
import research

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


def cleanup_old_reports(home_short: str, away_short: str, keep_filename: str | None = None) -> None:
    pattern = os.path.join(config.REPORTS_DIR, f"{home_short}_{away_short}_*.pdf")
    for path in glob.glob(pattern):
        if not keep_filename or os.path.basename(path) != keep_filename:
            try:
                os.remove(path)
            except Exception as e:
                logging.warning(f"Could not delete old report {path}: {e}")


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
    started = time.time()

    # Accept either JSON or form
    if request.content_type and request.content_type.startswith("application/x-www-form-urlencoded"):
        data = request.form.to_dict(flat=True)
    else:
        data = request.get_json(force=True, silent=True) or {}

    # 1) Auth
    user_api_key = _extract_api_key()
    if config.SERVICE_API_KEY and user_api_key != config.SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    # 2) Inputs
    home_full = data.get('home_full')
    away_full = data.get('away_full')
    home_short = data.get('home_short')
    away_short = data.get('away_short')
    if not all([home_full, away_full, home_short, away_short]):
        return jsonify({"error": "Missing team name parameters"}), 400

    # 3) Remove any existing report for this matchup before generating a new one
    cleanup_old_reports(home_short, away_short)

    today = datetime.now()
    date_str = db.format_friendly_date(today)
    filename = f"{home_short}_{away_short}_{date_str}.pdf"
    filepath = os.path.join(config.REPORTS_DIR, filename)

    # 4) API keys. OpenRouter is the sole LLM gateway; CFBD is the sole statistics source.
    cfbd_api_key = db.resolve_cfbd_key()
    openrouter_api_key = db.resolve_openrouter_key()
    missing = [n for n, v in (("CFBD", cfbd_api_key), ("OpenRouter", openrouter_api_key)) if not v]
    if missing:
        return jsonify({
            "error": "Missing required API keys",
            "missing": missing,
            "hint": "Add an 'openrouter' row to the API_KEYS table or set OPENROUTER_API_KEY.",
        }), 500

    # CFB seasons straddle the calendar year; January bowl games belong to the prior season.
    try:
        year = int(data.get('year')) if data.get('year') else cfbd.season_year(today)
    except (TypeError, ValueError):
        year = cfbd.season_year(today)

    ctx = research.build_context(
        home_full, away_full, home_short, away_short, year, kickoff=data.get('kickoff')
    )

    # 5) CFBD statistics and the eight live-web research calls have no dependency on each
    #    other, so run both stages concurrently — the report waits on the slower of the two,
    #    not on their sum.
    with ThreadPoolExecutor(max_workers=2) as pool:
        cfbd_future = pool.submit(cfbd.fetch_all, cfbd_api_key, year, home_short, away_short)
        research_future = pool.submit(research.run_research, openrouter_api_key, ctx)

        try:
            cfbd_data = cfbd_future.result()
        except Exception as e:
            logging.exception("CFBD fetch failed")
            return jsonify({"error": "CFBD data fetch failed", "detail": str(e)}), 502

        try:
            research_raw = research_future.result()
        except Exception:
            logging.exception("Research stage failed entirely")
            research_raw = {}

    stats = cfbd_data["stats"]

    # 6) Team metadata drives the header logos and every chart's colors.
    home_meta = cfbd.team_meta(cfbd_data["teams"], home_short)
    away_meta = cfbd.team_meta(cfbd_data["teams"], away_short)

    # 7) Season results, scoring rates, and the market line.
    home_games = cfbd.normalize_games(cfbd_data["games"]["teamA"], home_short)
    away_games = cfbd.normalize_games(cfbd_data["games"]["teamB"], away_short)
    home_profile = cfbd.scoring_profile(home_games)
    away_profile = cfbd.scoring_profile(away_games)
    market = cfbd.find_matchup_line(cfbd_data["lines"], home_short, away_short)

    # 8) National percentiles turn every advanced stat into "where does this rank".
    advanced = stats.get("Advanced Team Stats") or {}
    home_adv = (advanced.get("teamA") or [{}])[0] if advanced.get("teamA") else {}
    away_adv = (advanced.get("teamB") or [{}])[0] if advanced.get("teamB") else {}
    percentiles = cfbd.build_percentiles(cfbd_data["league"]["advanced"], home_adv, away_adv)

    # 9) Rotowire, filtered to the two teams in this matchup only.
    try:
        rotowire = {
            "home": db.fetch_rotowire_for_team(home_short, home_full),
            "away": db.fetch_rotowire_for_team(away_short, away_full),
        }
    except Exception as e:
        logging.warning(f"Rotowire lookup failed: {e}")
        rotowire = {"home": [], "away": []}

    # 10) Merge research + Rotowire into per-section buckets with deterministic [n] citations.
    registry = research.seed_registry()
    sections = research.assemble_sections(research_raw, rotowire, registry, ctx)

    # 11) Quantitative anchor for the prediction.
    baseline = predict.build_baseline(stats, home_profile, away_profile, market, home_short, away_short)

    # 12) Visuals.
    chart_set = charts_mod.build_all(
        stats, percentiles, baseline, home_meta, away_meta, home_short, away_short
    )

    # 13) Everything the report model is allowed to know.
    bundle = {
        "matchup": {
            "home_team": home_full,
            "home_short": home_short,
            "home_conference": home_meta.get("conference"),
            "away_team": away_full,
            "away_short": away_short,
            "away_conference": away_meta.get("conference"),
            "season": year,
            "kickoff": data.get("kickoff") or "",
            "generated_at_utc": ctx["now_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "statistics_cfbd": cfbd.prune_for_prompt(stats),
        "national_percentiles": percentiles,
        "season_results": {"home": home_games, "away": away_games},
        "scoring_profiles": {"home": home_profile, "away": away_profile},
        "betting_market": market,
        "statistical_baseline": baseline,
        "news_and_research": sections,
    }

    # 14) Single synthesis call — Kimi K3 via OpenRouter.
    try:
        result = report_mod.generate(openrouter_api_key, ctx, bundle, chart_set, registry)
    except Exception as e:
        logging.exception("Report generation failed")
        detail = getattr(e, "body", None) or str(e)
        return jsonify({"error": "Report model request failed", "detail": str(detail)[:800]}), 502

    report_text = result["text"]
    usage = result["usage"]

    # 15) Render.
    research_usage = [
        (r.get("usage") or {}) for r in research_raw.values() if isinstance(r, dict)
    ]
    research_in = sum(u.get("input_tokens") or 0 for u in research_usage)
    research_out = sum(u.get("output_tokens") or 0 for u in research_usage)
    sections_with_data = sum(
        1 for s in sections.values()
        if any(not b.get("no_data", True) for b in s["inputs"].values())
    )

    report_created = f"{db.format_friendly_date(today)} {today.strftime('%I:%M %p')}"
    meta_lines = [
        f"Research: {config.OPENROUTER_RESEARCH_MODEL} via OpenRouter — "
        f"{len(research.RESEARCH_JOBS)} live web searches, {sections_with_data}/{len(sections)} sections with findings "
        f"({research_in} in / {research_out} out tokens).",
        f"Report: {result['model']} via OpenRouter — "
        f"{usage.get('input_tokens') or 'N/A'} input tokens / {usage.get('output_tokens') or 'N/A'} output tokens.",
        f"Statistics: CollegeFootballData ({year} season). Visuals generated procedurally from those feeds.",
        f"Sources cited: {len(registry)}. Generation time: {int(time.time() - started)}s.",
    ]

    html_content = render.build_html(
        home_full=home_full,
        away_full=away_full,
        year=year,
        home_logo=home_meta.get("logo", ""),
        away_logo=away_meta.get("logo", ""),
        report_created=report_created,
        report_markdown=report_text,
        charts=chart_set,
        registry=registry,
        meta_lines=meta_lines,
    )

    try:
        render.write_pdf(html_content, filepath)
    except ImportError:
        return jsonify({"error": "PDF generation library not installed on server."}), 500
    except Exception as e:
        logging.error(f"PDF generation failed: {e}")
        return jsonify({"error": "PDF generation failed", "detail": str(e)}), 500

    # Stamp the AFPLNA watermark onto every page (post-process). A watermark problem must
    # never block report delivery, so failures here are logged and swallowed.
    if os.path.exists(config.WATERMARK_PATH):
        try:
            render.add_pdf_watermark(filepath, config.WATERMARK_PATH)
        except Exception as e:
            logging.warning(f"Watermark step failed; delivering report without watermark: {e}")
    else:
        logging.warning(f"Watermark image not found at {config.WATERMARK_PATH}; skipping watermark.")

    logging.info(
        f"Report {filename} generated in {int(time.time() - started)}s "
        f"({len(report_text)} chars, {len(registry)} sources)."
    )

    return jsonify({
        "message": "Report generated successfully",
        "filename": filename,
        "seconds": int(time.time() - started),
        "sources": len(registry),
        "sections_with_research": sections_with_data,
        "projected_score": baseline.get("projected_score"),
        "baseline_margin": baseline.get("consensus_margin"),
    }), 200


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
