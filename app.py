import os
import json
from datetime import datetime, timedelta
import logging
import glob
import time
import base64

import pymysql
import requests
from flask import Flask, request, send_file, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask_cors import CORS

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

# ---------------------------
# Helpers
# ---------------------------
REPORTS_DIR = os.path.join(os.getcwd(), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


def format_friendly_date(dt: datetime) -> str:
    """Return 'Month D, YYYY' without zero-padding the day, cross-platform."""
    try:
        return dt.strftime("%B %-d, %Y")
    except Exception:
        return dt.strftime("%B %d, %Y").replace(" 0", " ")


def cleanup_old_reports(home_short: str, away_short: str, keep_filename: str) -> None:
    pattern = os.path.join(REPORTS_DIR, f"{home_short}_{away_short}_*.pdf")
    for path in glob.glob(pattern):
        if os.path.basename(path) != keep_filename:
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
        deadline = time.time() + 90
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

        # Insert rows; open DB only when needed
        conn = get_db_connection()
        inserted = 0
        with conn.cursor() as cur:
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
                    "SELECT 1 FROM rotowire WHERE player_name=%s AND headline=%s AND team_name=%s "
                    "AND date_text=%s AND news_text=%s AND source_name=%s AND position=%s AND analysis_text=%s LIMIT 1",
                    (player_name, headline, team_name, date_text, news_text, source_name, position, analysis_text)
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    "INSERT INTO rotowire (player_name, headline, team_name, date_text, news_text, source_name, position, analysis_text) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (player_name, headline, team_name, date_text, news_text, source_name, position, analysis_text)
                )
                inserted += cur.rowcount or 0
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

    # 3) File path (today)
    today = datetime.now()
    date_str = format_friendly_date(today)
    filename = f"{home_short}_{away_short}_{date_str}.pdf"
    filepath = os.path.join(REPORTS_DIR, filename)

    if os.path.isfile(filepath):
        return jsonify({"message": "Report already exists", "filename": filename}), 200

    # 4) Load API keys (short-lived DB connection)
    cfbd_api_key = get_api_key('CFD') or get_api_key('CFBD') or os.getenv('CFBD_API_KEY')
    search_api_key = get_api_key('search') or os.getenv('GOOGLE_SEARCH_API_KEY')
    google_cx = get_api_key('google_cx') or os.getenv('GOOGLE_CX')
    bright_key = get_api_key('bright')
    gemini_api_key = get_api_key('google') or os.getenv('GOOGLE_API_KEY')

    if not all([cfbd_api_key, search_api_key, google_cx, bright_key, gemini_api_key]):
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

    # 6) Google CSE searches
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
    all_links: list[str] = []

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
            cleaned.append({
                "title": title,
                "snippet": snippet,
                "url": link,
                "displayLink": display,
            })
            if link:
                all_links.append(link)
        search_results[label] = cleaned

    # 7) Bright Data article scraping
    scraped_map: dict[str, dict] = {}
    if all_links:
        payload = [{"url": u} for u in all_links]
        trigger_endpoint = "https://api.brightdata.com/dca/trigger?collector=c_medq28351ctcsbh6vu"
        headers_bd = {"Authorization": f"Bearer {bright_key}", "Content-Type": "application/json"}
        trig_resp = requests.post(trigger_endpoint, json=payload, headers=headers_bd, timeout=45)
        if trig_resp.status_code == 200:
            cid = None
            try:
                cid = trig_resp.json().get("collection_id")
            except Exception:
                pass
            if cid:
                dataset_endpoint = f"https://api.brightdata.com/dca/dataset?id={cid}"
                deadline = time.time() + 120
                while time.time() < deadline:
                    ds_resp = requests.get(dataset_endpoint, headers={"Authorization": f"Bearer {bright_key}"}, timeout=30)
                    if ds_resp.status_code == 200 and ds_resp.text.strip():
                        try:
                            data_items = ds_resp.json()
                        except ValueError:
                            lines = ds_resp.text.strip().splitlines()
                            data_items = [json.loads(line) for line in lines if line.strip()]
                        if isinstance(data_items, list) and len(data_items) >= len(all_links):
                            for item in data_items:
                                url = item.get('source_url') or item.get('url') or (item.get('input', {}).get('url') if item.get('input') else '')
                                if not url:
                                    continue
                                text = item.get('article_text') or ""
                                title = item.get('title') or ""
                                published = item.get('published_time') or item.get('published') or ""
                                scraped_map[url] = {"ok": True, "text": text, "title": title, "published": published}
                            break
                    time.sleep(1)
        else:
            logging.error(f"Bright Data trigger failed: {trig_resp.status_code}, {trig_resp.text}")

    # Merge search + scraped
    for label, results in search_results.items():
        articles = []
        for res in results:
            url = res["url"]
            title = res["title"]
            snippet = res["snippet"]
            text = ""
            if url in scraped_map and scraped_map[url].get("ok"):
                text = scraped_map[url].get("text", "")
                if scraped_map[url].get("title"):
                    title = scraped_map[url]["title"]
            articles.append({"title": title or "", "text": text if text else snippet})
        fetchedData[label] = articles

    # 8) Injury news (open a FRESH DB connection now; the previous one may have timed out)
    injury_news: list[dict] = []
    dates = [format_friendly_date(datetime.now() - timedelta(days=i)) for i in range(7)]

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            conn.ping(reconnect=True)
            placeholders = ",".join(["%s"] * len(dates))
            query = f"SELECT player_name, headline, team_name, date_text, news_text, analysis_text FROM rotowire WHERE date_text IN ({placeholders})"
            # Retry once on transient disconnects
            for attempt in range(2):
                try:
                    cur.execute(query, tuple(dates))
                    rows = cur.fetchall() or []
                    break
                except pymysql.err.OperationalError as e:
                    if attempt == 0 and e.args and e.args[0] in (2006, 2013):
                        time.sleep(1)
                        conn.ping(reconnect=True)
                        continue
                    raise
        for (player, headline, team, date_text, news_text, analysis_text) in rows:
            injury_news.append({
                "team": team,
                "player": player,
                "headline": headline,
                "news": news_text,
                "analysis": analysis_text,
            })
    finally:
        try:
            conn.close()
        except Exception:
            pass

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

    # 10) Build LLM prompt & call Gemini
    prompt_intro = (
        f"You are a top-tier, seasoned sports analyst. Using the provided CFD statistics and news articles to craft a full-length matchup report for {home_full} vs {away_full} in {year}. You speak in the voice and style of a seasoned sports analyst, handicapper, and writer. "
        f"Create a dedicated section for each of the following data groups: SP Ratings, ELO Ratings, FPI Ratings, Advanced Team Stats, Returning Production, Team Talent, Team PPA, Player PPA, Team Season Stats, Adjusted Team Metrics, {home_full} injury updates, {away_full} injury updates, {home_full} roster Updates, {away_full} Roster Updates, {home_full} practice and Scrimmage updates, {away_full} practice and scrimmage updates, {home_full} vs {away_full} Media Matchup Analysis (This is a summary on the the matchup news articles provided). "
        f"For every section list key statistics followed by at least two in-depth paragraphs analyzing how those numbers impact the game. Use the confident, authoritative tone of a national sports analyst. The final section should be called Conclusion, and deliver your overall verdict and a projected point spread based on all data that you have. Remember this is your estimated point spread, and you will be evaluated based on how close you are to the actual final score. \n\n"
        f"Note: When reviewing the The injury News, you should only include items within the Last 7 Days and data from the 2 teams in teh matchup. Do not summarize or include any data that is not specific to this matchup. Only use news items relevant to {home_full} or {away_full} when writing the sections. Do not include or take into account content about former players, or news stories not relevant to the two current teams that would/could impact performance. The purpose of this report is to give the reader the best and most relevant information needed to make a decision on the outcome of this specific game. In addition, if there is no information or statistics provided for one of the specified sections, keep the section header, but only note to the reader that their was no data available for that section. Do not write any more details that that notification. \n\n"
        f"Data: "
    )
    prompt = prompt_intro + json.dumps(fetchedData)

    gemini_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.7},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": gemini_api_key}

    ai_resp = requests.post(gemini_url, json=body, headers=headers, timeout=120)
    if ai_resp.status_code != 200:
        return jsonify({"error": "Gemini API request failed", "detail": ai_resp.text}), 502

    try:
        result = ai_resp.json()
        report_text = result['candidates'][0]['content']['parts'][0]['text']
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
        cleanup_old_reports(home_short, away_short, filename)
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

    today = datetime.now()
    date_str = format_friendly_date(today)
    filename = f"{home_short}_{away_short}_{date_str}.pdf"
    filepath = os.path.join(REPORTS_DIR, filename)

    if not os.path.isfile(filepath):
        return jsonify({"error": "Report not found. Please generate it first."}), 404

    return send_file(filepath, mimetype='application/pdf', as_attachment=True, download_name=filename)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
