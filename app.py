import os
import json
from datetime import datetime, timedelta
import logging
import glob
import time
import base64

import pymysql
import sqlite3
import requests
from flask import Flask, request, send_file, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

# ---------------------------
# App & CORS
# ---------------------------
app = Flask(__name__)
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")
CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGIN}}, supports_credentials=False)

try:
    import markdown  # optional for Markdown -> HTML
except ImportError:
    markdown = None

logging.basicConfig(level=logging.INFO)

# ---------------------------
# Env config
# ---------------------------
DB_HOST = os.getenv('DB_HOST', 'p3nlmysql149plsk.secureserver.net')
DB_USER = os.getenv('DB_USER', 'kdogg4207')
DB_NAME = os.getenv('DB_NAME', 'kdogg4207')
DB_PASSWORD = os.getenv('DB_PASSWORD')
SERVICE_API_KEY = os.getenv('SERVICE_API_KEY')
WKHTMLTOPDF_PATH = os.getenv('WKHTMLTOPDF_PATH')  # /usr/bin/wkhtmltopdf
# Local Rotowire DB path; defaults to 'rotowire.db' in the project root
ROTOWIRE_DB_PATH = os.getenv('ROTOWIRE_DB_PATH', os.path.join(os.getcwd(), 'rotowire.db'))

# ---------------------------
# Helpers
# ---------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.getenv("REPORTS_DIR", os.path.join(BASE_DIR, "reports"))
os.makedirs(REPORTS_DIR, exist_ok=True)


def format_friendly_date(dt: datetime) -> str:
    """Return 'Month D, YYYY' without zero-padding the day, cross-platform."""
    try:
        return dt.strftime("%B %-d, %Y")
    except Exception:
        return dt.strftime("%B %d, %Y").replace(" 0", " ")


def cleanup_old_reports(home_short: str, away_short: str, keep_filename: str | None = None) -> None:
    pattern = os.path.join(REPORTS_DIR, f"{home_short}_{away_short}_*.pdf")
    for path in glob.glob(pattern):
        if not keep_filename or os.path.basename(path) != keep_filename:
            try:
                os.remove(path)
            except Exception as e:
                logging.warning(f"Could not delete old report {path}: {e}")


def get_db_connection():
    """Return a MySQL connection configured for long-running operations.
    We DO NOT keep connections open while doing network calls; open only when needed.
    """
    conn = pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset='utf8mb4',
        autocommit=True,
        connect_timeout=15,
        read_timeout=600,
        write_timeout=600,
    )
    # Attempt to raise per-connection server-side timeouts (allowed on many shared hosts)
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION net_read_timeout=600, net_write_timeout=600")
            try:
                cur.execute("SET SESSION wait_timeout=600")
            except Exception:
                pass
    except Exception:
        pass
    return conn


def get_rotowire_db_connection():
    """Return a connection to the local SQLite Rotowire database.
    Ensures the table structure exists."""
    db_dir = os.path.dirname(ROTOWIRE_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(ROTOWIRE_DB_PATH)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rotowire (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name TEXT,
                headline TEXT,
                team_name TEXT,
                date_text TEXT,
                news_text TEXT,
                source_name TEXT,
                position TEXT,
                analysis_text TEXT
            )
            """
        )
    return conn


def get_api_key(name: str) -> str | None:
    """Fetch an API key from API_KEYS table; returns stripped string or None."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT `KEY` FROM API_KEYS WHERE API_NAME=%s LIMIT 1", (name,))
            row = cur.fetchone()
            if not row:
                return None
            # row is a tuple (key,) by default
            key = row[0]
            if key and str(key).strip():
                return str(key).strip()
            return None
    finally:
        conn.close()


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
        bright_key = get_api_key('bright')
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

        # Poll up to 90s
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
        conn = get_rotowire_db_connection()
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
    data = request.get_json(force=True)

    # 1) Auth
    user_api_key = data.get('api_key')
    if SERVICE_API_KEY and user_api_key != SERVICE_API_KEY:
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

    # Filename for new report (today)
    today = datetime.now()
    date_str = format_friendly_date(today)
    filename = f"{home_short}_{away_short}_{date_str}.pdf"
    filepath = os.path.join(REPORTS_DIR, filename)

    # 4) Load API keys (short-lived DB connection)
    cfbd_api_key = get_api_key('CFD') or get_api_key('CFBD') or os.getenv('CFBD_API_KEY')
    search_api_key = get_api_key('search') or os.getenv('GOOGLE_SEARCH_API_KEY')
    google_cx = get_api_key('google_cx') or os.getenv('GOOGLE_CX')
    gemini_api_key = get_api_key('google') or os.getenv('GOOGLE_API_KEY')

    if not all([cfbd_api_key, search_api_key, google_cx, gemini_api_key]):
        return jsonify({"error": "Missing required API keys"}), 500

    # 5) CFBD stats (external calls; no DB connection held)
    headers = {"Authorization": f"Bearer {cfbd_api_key}"}
    year = datetime.now().year
    fetchedData: dict[str, object] = {}

    stat_endpoints = [
        ("/ratings/sp",             "SP Ratings"),
        ("/ratings/elo",            "ELO Ratings"),
        ("/ratings/fpi",            "FPI Ratings"),
        ("/stats/season/advanced",  "Advanced Team Stats"),
        ("/player/returning",       "Returning Production"),
        ("/talent",                 "Team Talent"),
        ("/ppa/games",              "Team PPA"),
        ("/ppa/players/season",     "Player PPA"),
        ("/stats/season",           "Team Season Stats"),
        ("/wepa/team/season",       "Adjusted Team Metrics"),
    ]
    base_url = "https://api.collegefootballdata.com"

    for endpoint, label in stat_endpoints:
        try:
            resA = requests.get(base_url + endpoint, headers=headers,
                                params={"year": year, "team": home_short}, timeout=30)
            dataA = resA.json() if resA.status_code == 200 else []
        except Exception as e:
            logging.warning(f"CFBD {label} for {home_short} failed: {e}")
            dataA = []

        try:
            resB = requests.get(base_url + endpoint, headers=headers,
                                params={"year": year, "team": away_short}, timeout=30)
            dataB = resB.json() if resB.status_code == 200 else []
        except Exception as e:
            logging.warning(f"CFBD {label} for {away_short} failed: {e}")
            dataB = []

        fetchedData[label] = {"teamA": dataA, "teamB": dataB}

    # 6) Google CSE searches (unchanged definitions, but we'll cap to 20 URLs overall)
    categories = {
        "Team A injury updates":                 f'Latest football injury news for the "{home_full}" team. Only the latest news associated with the team, no more than 14 days old.',
        "Team B injury updates":                 f'Latest football injury news for the "{away_full}" team. Only the latest news associated with the team, no more than 14 days old.',
        "Team A roster Updates":                 f'Latest football roster news for the "{home_full}" team. Only the latest news associated with the team, no more than 14 days old. Only current players.',
        "Team B Roster Updates":                 f'Latest football roster news for the "{away_full}" team. Only the latest news associated with the team, no more than 14 days old. Only current players.',
        "Team A practice and Scrimmage updates": f'Latest "{home_full}" football practice and scrimmage report. no more than 7 days old.',
        "Team B practice and scrimmage updates": f'Latest "{away_full}" football practice and scrimmage report. no more than 7 days old.',
        "Matchup Analysis":                      f'The upcoming "{home_full}" vs "{away_full}" matchup. The latest expert football predictions and analysis.',
    }

    search_results: dict[str, list[dict[str, str]]] = {}
    raw_links: list[tuple[str, str]] = []  # (category, url)

    for label, query in categories.items():
        params = {
            "key": search_api_key,
            "cx": google_cx,
            "q": query,
            "num": 5 if label == "Matchup Analysis" else 3,
            "gl": "us",
            "hl": "en",
            "dateRestrict": "d7",
        }
        resp = requests.get("https://customsearch.googleapis.com/customsearch/v1", params=params, timeout=30)
        if resp.status_code != 200:
            return jsonify({"error": f"Google search failed for {label}", "detail": resp.text}), 502
        data = resp.json()
        items = data.get("items", [])
        cleaned = []
        for item in items:
            link = item.get("link", "")
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            display = item.get("displayLink", "")
            if not link:
                continue
            cleaned.append({"title": title, "snippet": snippet, "url": link, "displayLink": display, "category": label})
            raw_links.append((label, link))
        search_results[label] = cleaned

    # Cap to <= 20 total URLs for URL context (preferring Matchup Analysis first, then others)
    def dedupe_ordered(pairs: list[tuple[str, str]]):
        seen = set()
        out = []
        for cat, u in pairs:
            if u not in seen:
                out.append((cat, u))
                seen.add(u)
        return out

    # Preference order: Matchup first, then injuries/roster/practice buckets
    priority = ["Matchup Analysis",
                "Team A injury updates", "Team B injury updates",
                "Team A roster Updates", "Team B Roster Updates",
                "Team A practice and Scrimmage updates", "Team B practice and scrimmage updates"]

    raw_links = [p for p in raw_links if p[0] in priority]
    raw_links = sorted(raw_links, key=lambda p: priority.index(p[0]))
    raw_links = dedupe_ordered(raw_links)

    MAX_URLS = 20
    urls_for_ai_pairs = raw_links[:MAX_URLS]
    urls_for_ai = [u for _, u in urls_for_ai_pairs]

    # 7) Build a compact article source list for the prompt (no scraping)
    articles_struct = []
    for label, results in search_results.items():
        for r in results:
            if r["url"] in urls_for_ai:
                articles_struct.append({
                    "category": label,
                    "title": r["title"],
                    "url": r["url"],
                    "snippet": r["snippet"],
                })

    # If you want to see what we'll ask Gemini to read:
    fetchedData["Media Sources"] = articles_struct

    # 8) Injury news (open a FRESH DB connection now; the previous one may have timed out)
    injury_news: list[dict] = []
    dates = [format_friendly_date(datetime.now() - timedelta(days=i)) for i in range(7)]

    conn = get_rotowire_db_connection()
    try:
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(dates))
        query = (
            "SELECT player_name, headline, team_name, date_text, news_text, analysis_text "
            f"FROM rotowire WHERE date_text IN ({placeholders})"
        )
        cur.execute(query, dates)
        rows = cur.fetchall() or []
        for (player, headline, team, date_text, news_text, analysis_text) in rows:
            injury_news.append({
                "team": team,
                "player": player,
                "headline": headline,
                "news": news_text,
                "analysis": analysis_text,
            })
        cur.close()
    finally:
        conn.close()

    fetchedData["Injury News Last 7 Days"] = injury_news

    # 9) Team logos from CFBD
    home_logo = ""
    away_logo = ""
    try:
        teams_resp = requests.get(
            "https://api.collegefootballdata.com/teams/fbs",
            headers={"Authorization": f"Bearer {cfbd_api_key}"},
            params={"year": year},
            timeout=20,
        )
        teams_list = teams_resp.json() if teams_resp.status_code == 200 else []
        for team in teams_list:
            if team.get("school") == home_short and team.get("logos"):
                home_logo = team["logos"][0]
            if team.get("school") == away_short and team.get("logos"):
                away_logo = team["logos"][0]
    except Exception as e:
        logging.warning(f"Could not retrieve team logos from CFBD: {e}")

    # 10) Build LLM prompt & call Gemini (URL context enabled)
    prompt_intro = (
        f"You are a top-tier, seasoned sports analyst. Using the provided CFD statistics and news articles to craft a full-length matchup report for {home_full} vs {away_full} in {year}. You speak in the voice and style of a seasoned sports analyst, handicapper, and writer. "
        f"You have the url_context tool enabled—READ the linked articles directly and prefer their actual content over snippets. "
        f"Create a dedicated section for each of the following data groups: Matchup Overview (Provide an introduction to the matchup, with some context and history of the matchup to set the stage), SP Ratings (Note: For SP Ratings, do not factor in the ranking value, it will always be 1 and is irrelivent. Only factor in the the other values provided), ELO Ratings, FPI Ratings, Advanced Team Stats, Returning Production, Team Talent, Team PPA, Player PPA, Team Season Stats, Adjusted Team Metrics, {home_full} injury updates, {away_full} injury updates, {home_full} roster Updates, {away_full} Roster Updates, {home_full} practice and Scrimmage updates, {away_full} practice and scrimmage updates, {home_full} vs {away_full} Media Matchup Analysis (This is a summary on the the matchup news articles provided), and Final Prediction. "
        f"For every section list key statistics (where applicable) followed by at least two in-depth paragraphs analyzing how those numbers impact the game. Use the confident, authoritative tone of a national sports analyst. The final section should be called Final Prediction, and deliver your overall verdict and a projected point spread based on all data that you have. Remember this is YOUR personal estimated point spread, and you will be evaluated based on how close you are to the actual final score. If you want to be the best, then you have to really be accurate! \n\n"
        f"Note: When reviewing the The injury News, you should only include items within the Last 7 Days and data from the 2 teams in the matchup. Do not summarize or include any data that is not specific to this matchup. Only use news items relevant to {home_full} or {away_full} when writing the sections. Do not include or take into account content about former players, or news stories not relevant to the two current teams that would/could impact performance. The purpose of this report is to give the reader the best and most relevant information needed to make a decision on the outcome of this specific game. In addition, if there is no information or statistics provided for one of the specified sections, keep the section header, but only note to the reader that their was no data available for that section. Do not write any more details that that notification. \n\n"
        f"Data: "
    )

    # List the URLs explicitly (this is what URL context will fetch)
    url_list_text = "Sources to read (max 20):\n" + "\n".join(f"- {a['category']}: {a['url']}" for a in articles_struct)

    # Keep your existing data bundle (stats + injuries + our media source list)
    data_blob = json.dumps(fetchedData)

    prompt = prompt_intro + url_list_text + "\n\n" + data_blob + "\n\n" + (
        "Citations rule: when you reference specific claims from an article, add an inline marker like [1], [2], etc., "
        "and include a short SOURCES section at the end mapping [n] -> URL. Also, always add collegefootballdata.com and rotowire.com as sources in teh source list at the end of the report. In addition, never explain the url process or what urlls could not be accessed, just exclude the urls that could not be accessed from teh sources at the end."
    )

    gemini_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.7},
        # >>> Enable URL context <<<
        "tools": [
            {"url_context": {}}
            # Optionally also allow search grounding:
            # , {"google_search": {}}
        ],
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": gemini_api_key}

    ai_resp = requests.post(gemini_url, json=body, headers=headers, timeout=120)
    if ai_resp.status_code != 200:
        return jsonify({"error": "Gemini API request failed", "detail": ai_resp.text}), 502

    try:
        result = ai_resp.json()
        report_text = result['candidates'][0]['content']['parts'][0]['text']
        # For debugging/auditing, you can log which URLs the tool actually retrieved:
        used_url_meta = result['candidates'][0].get('url_context_metadata', {})
        logging.info(f"URL context used: {used_url_meta}")
    except Exception:
        return jsonify({"error": "Unexpected response format from Gemini", "response": ai_resp.text[:800]}), 502

    # 11) Markdown -> HTML body
    if markdown:
        report_html_body = markdown.markdown(report_text)
    else:
        report_html_body = "<br>\n".join(report_text.split("\n"))

    html_content = f"""
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <title>Matchup Report</title>
      <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.45; }}
        h1, h2, h3 {{ margin: 0.2em 0; }}
        .hdr {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; background-color:#333; padding:20px; color:white; }}
        .hdr img {{ width:100px; height:100px; object-fit:contain; }}
        .content {{ text-align:left; }}
      </style>
    </head>
    <body>
      <div class=\"hdr\">
        <img src=\"{home_logo}\" alt=\"{home_full} logo\">
        <div style=\"text-align:center; flex-grow:1;\">
            <h1>AFPLNA College Football Matchup Report</h1>
            <h2>{home_full} vs {away_full} ({year})</h2>
        </div>
        <img src=\"{away_logo}\" alt=\"{away_full} logo\">
      </div>
      <div class=\"content\">{report_html_body}</div>
    </body>
    </html>
    """

    # 12) HTML -> PDF
    try:
        import pdfkit
    except ImportError:
        return jsonify({"error": "PDF generation library not installed on server."}), 500

    pdfkit_config = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH) if WKHTMLTOPDF_PATH else None
    try:
        pdfkit.from_string(html_content, filepath, configuration=pdfkit_config)
    except Exception as e:
        logging.error(f"PDF generation failed: {e}")
        return jsonify({"error": "PDF generation failed", "detail": str(e)}), 500

    return jsonify({"message": "Report generated successfully", "filename": filename}), 200


@app.route('/get-report', methods=['GET'])
def get_report():
    api_key_param = request.args.get('api_key')
    if SERVICE_API_KEY and api_key_param != SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    home_short = request.args.get('home_team')
    away_short = request.args.get('away_team')
    if not home_short or not away_short:
        return jsonify({"error": "Missing team name parameters"}), 400

    pattern = os.path.join(REPORTS_DIR, f"{home_short}_{away_short}_*.pdf")
    files = glob.glob(pattern)
    if not files:
        return jsonify({"error": "Report not found. Please generate it first."}), 404

    filepath = max(files, key=os.path.getmtime)
    filename = os.path.basename(filepath)

    return send_file(filepath, mimetype='application/pdf', as_attachment=True, download_name=filename)


@app.route('/has-report', methods=['GET'])
def has_report():
    api_key_param = request.args.get('api_key')
    if SERVICE_API_KEY and api_key_param != SERVICE_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    home_short = request.args.get('home_team')
    away_short = request.args.get('away_team')
    if not home_short or not away_short:
        return jsonify({"error": "Missing team name parameters"}), 400

    pattern = os.path.join(REPORTS_DIR, f"{home_short}_{away_short}_*.pdf")
    files = glob.glob(pattern)
    logging.info(f"[has-report] REPORTS_DIR={REPORTS_DIR} pattern={pattern} matches={len(files)}")
    return jsonify({"exists": bool(files)}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
