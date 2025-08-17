import os
import json
from datetime import datetime, timedelta
import base64
import logging

import pymysql
import requests
from flask import Flask, request, send_file, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask_cors import CORS


ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")  # set this to your site domain later
CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGIN}}, supports_credentials=False)

try:
    import markdown  # for converting report Markdown to HTML
except ImportError:
    markdown = None

# Configure logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# Load configuration from environment variables
DB_HOST = os.getenv('DB_HOST', 'p3nlmysql149plsk.secureserver.net')
DB_USER = os.getenv('DB_USER', 'kdogg4207')
DB_NAME = os.getenv('DB_NAME', 'kdogg4207')
DB_PASSWORD = os.getenv('DB_PASSWORD')             # database password
API_KEY_ENV = os.getenv('SERVICE_API_KEY')         # API key for authenticating requests

# Ensure reports directory exists
REPORTS_DIR = os.path.join(os.getcwd(), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# Helper: get a new database connection
def get_db_connection():
    return pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
                           database=DB_NAME, charset='utf8mb4')

# Helper: fetch an API key value from the API_KEYS table
def get_api_key(name, conn):
    with conn.cursor() as cur:
        sql = "SELECT `KEY` FROM API_KEYS WHERE API_NAME=%s LIMIT 1"
        cur.execute(sql, (name,))
        row = cur.fetchone()
        if row:
            key = row[0]
            if key and key.strip():
                return key.strip()
    return None

# Scheduled Rotowire scrape job (runs at 9:00 and 18:00 every day)
sched = BackgroundScheduler(timezone="America/New_York")

@sched.scheduled_job(CronTrigger(hour=9, minute=0))
@sched.scheduled_job(CronTrigger(hour=18, minute=0))
def scheduled_rotowire_job():
    try:
        logging.info("Starting scheduled Rotowire scrape job...")
        conn = get_db_connection()
        bright_key = get_api_key('bright', conn)
        if not bright_key:
            logging.error("Bright Data API key not found. Rotowire scrape aborted.")
            conn.close()
            return
        # Trigger Bright Data collector for Rotowire
        collector_id = 'c_meewnv1y2gctpr239v'  # from original code
        trigger_url = f"https://api.brightdata.com/dca/trigger?queue_next=1&collector={collector_id}"
        headers = {
            "Authorization": f"Bearer {bright_key}",
            "Content-Type": "application/json"
        }
        # Bright Data expects a JSON array payload (even empty)
        trigger_resp = requests.post(trigger_url, json=[{}], headers=headers, timeout=30)
        if trigger_resp.status_code != 200:
            logging.error(f"Failed to trigger Rotowire scrape. Status: {trigger_resp.status_code}, Response: {trigger_resp.text}")
            conn.close()
            return
        data = trigger_resp.json()
        collection_id = data.get('collection_id')
        if not collection_id:
            logging.error("No collection_id returned from Bright Data trigger.")
            conn.close()
            return
        dataset_url = f"https://api.brightdata.com/dca/dataset?id={collection_id}"
        # Poll for dataset readiness (max 20 seconds)
        rotowire_data = None
        for _ in range(10):
            resp = requests.get(dataset_url, headers={"Authorization": f"Bearer {bright_key}"}, timeout=10)
            if resp.status_code == 200 and resp.text.strip():
                try:
                    rotowire_data = resp.json()
                except ValueError:
                    # Handle newline-delimited JSON
                    lines = resp.text.strip().splitlines()
                    rotowire_data = [json.loads(line) for line in lines if line.strip()]
                if rotowire_data is not None:
                    break
            # Not ready, wait 2 seconds
            import time
            time.sleep(2)
        if not rotowire_data:
            logging.error("Rotowire data not ready or empty.")
            conn.close()
            return
        # Insert new records into rotowire table
        inserted_count = 0
        with conn.cursor() as cur:
            for entry in rotowire_data:
                player_name = entry.get('player_name', '') or ''
                headline    = entry.get('headline', '') or ''
                team_name   = entry.get('team_name', '') or ''
                date_text   = entry.get('date_text', '') or ''
                news_text   = entry.get('news_text', '') or ''
                source_name = entry.get('source_name', '') or ''
                position    = entry.get('position', '') or ''
                analysis    = entry.get('analysis_text', '') or ''
                # Check if this exact news entry already exists
                select_sql = ("SELECT 1 FROM rotowire WHERE player_name=%s AND headline=%s AND team_name=%s "
                              "AND date_text=%s AND news_text=%s AND source_name=%s AND position=%s AND analysis_text=%s LIMIT 1")
                cur.execute(select_sql, (player_name, headline, team_name, date_text, news_text, source_name, position, analysis))
                if cur.fetchone():
                    continue  # skip duplicates
                insert_sql = ("INSERT INTO rotowire "
                              "(player_name, headline, team_name, date_text, news_text, source_name, position, analysis_text) "
                              "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)")
                cur.execute(insert_sql, (player_name, headline, team_name, date_text, news_text, source_name, position, analysis))
                if cur.rowcount > 0:
                    inserted_count += 1
            conn.commit()
        logging.info(f"Rotowire scrape completed. Inserted {inserted_count} new records.")
    except Exception as e:
        logging.exception("Error during Rotowire scheduled job: %s", e)
    finally:
        try:
            conn.close()
        except:
            pass

# Start the scheduler
sched.start()

@app.route('/generate-report', methods=['POST'])
def generate_report():
    data = request.get_json(force=True)
    # 1. Authenticate the API key
    user_api_key = data.get('api_key')
    if API_KEY_ENV and user_api_key != API_KEY_ENV:
        return jsonify({"error": "Unauthorized"}), 401
    # 2. Parse input team names
    home_full  = data.get('home_full')
    away_full  = data.get('away_full')
    home_short = data.get('home_short')
    away_short = data.get('away_short')
    if not home_full or not away_full or not home_short or not away_short:
        return jsonify({"error": "Missing team name parameters"}), 400
    # Construct filename for today's report
    today = datetime.now()
    # Format date like "August 17, 2025" (avoid zero-padding day)
    date_str = today.strftime("%B %-d, %Y") if '//' not in '//' else today.strftime("%B %d, %Y").replace(" 0", " ")
    filename = f"{home_short}_{away_short}_{date_str}.pdf"
    filepath = os.path.join(REPORTS_DIR, filename)
    # 3. If report already exists for today, abort generation
    if os.path.isfile(filepath):
        return jsonify({"message": "Report already exists", "filename": filename}), 200

    conn = get_db_connection()
    try:
        # 4. Load required API keys from DB
        cfbd_api_key   = get_api_key('CFD', conn) or get_api_key('CFBD', conn) or os.getenv('CFBD_API_KEY')
        search_api_key = get_api_key('search', conn) or os.getenv('GOOGLE_SEARCH_API_KEY')
        google_cx      = get_api_key('google_cx', conn) or os.getenv('GOOGLE_CX')
        bright_key     = get_api_key('bright', conn)
        gemini_api_key = get_api_key('google', conn) or os.getenv('GOOGLE_API_KEY')
        if not all([cfbd_api_key, search_api_key, google_cx, bright_key, gemini_api_key]):
            return jsonify({"error": "Missing required API keys"}), 500

        # 5. Fetch CFBD stats for each category
        headers = {"Authorization": f"Bearer {cfbd_api_key}"}
        year = datetime.now().year
        fetchedData = {}
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
            ("/wepa/team/season",       "Adjusted Team Metrics")
        ]
        base_url = "https://api.collegefootballdata.com"
        for endpoint, label in stat_endpoints:
            # Home team data
            resA = requests.get(base_url + endpoint, headers=headers, params={"year": year, "team": home_short}, timeout=15)
            dataA = resA.json() if resA.status_code == 200 else []
            # Away team data
            resB = requests.get(base_url + endpoint, headers=headers, params={"year": year, "team": away_short}, timeout=15)
            dataB = resB.json() if resB.status_code == 200 else []
            fetchedData[label] = {"teamA": dataA, "teamB": dataB}

        # 6. Google CSE searches for news articles in each category
        categories = {
            "Team A injury updates":            f"\"{home_full}\" football injury report",
            "Team B injury updates":            f"\"{away_full}\" football injury report",
            "Team A roster Updates":            f"\"{home_full}\" football roster news",
            "Team B Roster Updates":            f"\"{away_full}\" football roster news",
            "Team A practice and Scrimmage updates":  f"\"{home_full}\" football practice scrimmage report",
            "Team B practice and scrimmage updates":  f"\"{away_full}\" football practice scrimmage report",
            "Matchup Analysis":                f"\"{home_full}\" vs \"{away_full}\" football predictions analysis"
        }
        search_results = {}
        all_links = []
        for label, query in categories.items():
            params = {
                "key": search_api_key,
                "cx": google_cx,
                "q": query,
                "num": 5 if label == "Matchup Analysis" else 3,
                "gl": "us",
                "hl": "en",
                "dateRestrict": "d7"
            }
            resp = requests.get("https://customsearch.googleapis.com/customsearch/v1", params=params, timeout=15)
            if resp.status_code != 200:
                return jsonify({"error": f"Google search failed for {label}", "detail": resp.text}), 502
            data = resp.json()
            items = data.get("items", [])
            cleaned_results = []
            for item in items:
                link    = item.get("link", "")
                title   = item.get("title", "")
                snippet = item.get("snippet", "")
                display = item.get("displayLink", "")
                cleaned_results.append({
                    "title": title,
                    "snippet": snippet,
                    "url": link,
                    "displayLink": display
                })
                if link:
                    all_links.append(link)
            search_results[label] = cleaned_results

        # 7. Bright Data scraping for each article link
        scraped_map = {}
        if all_links:
            payload = [{"url": u} for u in all_links]
            trigger_endpoint = "https://api.brightdata.com/dca/trigger?collector=c_medq28351ctcsbh6vu"
            headers = {
                "Authorization": f"Bearer {bright_key}",
                "Content-Type": "application/json"
            }
            trig_resp = requests.post(trigger_endpoint, json=payload, headers=headers, timeout=30)
            if trig_resp.status_code == 200:
                cid = trig_resp.json().get("collection_id")
                if cid:
                    dataset_endpoint = f"https://api.brightdata.com/dca/dataset?id={cid}"
                    deadline = datetime.now() + timedelta(seconds=90)
                    while datetime.now() < deadline:
                        ds_resp = requests.get(dataset_endpoint, headers={"Authorization": f"Bearer {bright_key}"}, timeout=15)
                        if ds_resp.status_code == 200 and ds_resp.text.strip():
                            try:
                                data_items = ds_resp.json()
                            except ValueError:
                                # handle NDJSON
                                lines = ds_resp.text.strip().splitlines()
                                data_items = [json.loads(line) for line in lines if line.strip()]
                            if isinstance(data_items, list) and len(data_items) >= len(all_links):
                                # Process each returned item
                                for item in data_items:
                                    url = item.get('source_url') or item.get('url') or (item.get('input', {}).get('url') if item.get('input') else '')
                                    if not url:
                                        continue
                                    text = item.get('article_text') or ""
                                    title = item.get('title') or ""
                                    published = item.get('published_time') or item.get('published') or ""
                                    scraped_map[url] = {
                                        "ok": True,
                                        "text": text,
                                        "title": title,
                                        "published": published
                                    }
                                break
                        # wait 0.5s and retry
                        import time
                        time.sleep(0.5)
            else:
                logging.error(f"Bright Data trigger failed: {trig_resp.status_code}, {trig_resp.text}")
        # Combine search results with scraped text
        for label, results in search_results.items():
            articles = []
            for res in results:
                url    = res["url"]
                title  = res["title"]
                snippet= res["snippet"]
                text   = ""
                if url in scraped_map and scraped_map[url].get("ok"):
                    text = scraped_map[url].get("text", "")
                    if scraped_map[url].get("title"):
                        title = scraped_map[url]["title"]
                articles.append({
                    "title": title or "",
                    "text": text if text else snippet
                })
            fetchedData[label] = articles

        # 8. Include last 7 days of injury news from DB (rotowire)
        dates = []
        for i in range(7):
            d = datetime.now() - timedelta(days=i)
            ds = d.strftime("%B %-d, %Y") if '//' not in '//' else d.strftime("%B %d, %Y").replace(" 0", " ")
            dates.append(ds)
        injury_news = []
        with conn.cursor() as cur:
            # Select any rotowire news whose date_text matches one of the last 7 days
            format_list = ",".join(["%s"] * len(dates))
            query = f"SELECT player_name, headline, team_name, date_text, news_text, analysis_text FROM rotowire WHERE date_text IN ({format_list})"
            cur.execute(query, tuple(dates))
            rows = cur.fetchall()
            for (player, headline, team, date_text, news_text, analysis_text) in rows:
                injury_news.append({
                    "team": team,
                    "player": player,
                    "headline": headline,
                    "news": news_text,
                    "analysis": analysis_text
                })
        fetchedData["Injury News Last 7 Days"] = injury_news

    finally:
        conn.close()

    # 9. Get team logos via CFBD to include in the PDF (for header)
    home_logo = away_logo = ""
    try:
        teams_resp = requests.get("https://api.collegefootballdata.com/teams/fbs",
                                  headers={"Authorization": f"Bearer {cfbd_api_key}"},
                                  params={"year": year}, timeout=10)
        teams_list = teams_resp.json() if teams_resp.status_code == 200 else []
        for team in teams_list:
            if team.get("school") == home_short and team.get("logos"):
                home_logo = team["logos"][0]
            if team.get("school") == away_short and team.get("logos"):
                away_logo = team["logos"][0]
    except Exception as e:
        logging.warning(f"Could not retrieve team logos from CFBD: {e}")

    # 10. Construct AI prompt and call Gemini API
    prompt_intro = (
        f"You are a top-tier, seasoned sports analyst. Using the provided CFD statistics and news articles, craft a full-length matchup report for {home_full} vs {away_full} in {year}. "
        f"Create a dedicated section for each of the following data groups: SP Ratings, ELO Ratings, FPI Ratings, Advanced Team Stats, Returning Production, Team Talent, Team PPA, Player PPA, Team Season Stats, Adjusted Team Metrics, Team A injury updates, Team B injury updates, Team A roster Updates, Team B Roster Updates, Team A practice and Scrimmage updates, Team B practice and scrimmage updates, Matchup Analysis. "
        f"For every section list key statistics followed by at least two in-depth paragraphs analyzing how those numbers impact the game. Use the confident, authoritative tone of a national sports analyst. The final section should deliver your overall verdict and a projected point spread based on all data.\n\n"
        f"Note: The 'Injury News Last 7 Days' data includes all teams; only use news items relevant to {home_full} or {away_full} when writing the injury update sections.\n\n"
        f"Data: "
    )
    prompt = prompt_intro + json.dumps(fetchedData)
    gemini_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    body = {
        "contents": [ { "parts": [ { "text": prompt } ] } ],
        "generationConfig": { "maxOutputTokens": 8192, "temperature": 0.7 }
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": gemini_api_key
    }
    ai_resp = requests.post(gemini_url, json=body, headers=headers, timeout=60)
    if ai_resp.status_code != 200:
        return jsonify({"error": "Gemini API request failed", "detail": ai_resp.text}), 502
    result = ai_resp.json()
    try:
        report_text = result['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError, TypeError):
        return jsonify({"error": "Unexpected response format from Gemini", "response": result}), 502

    # 11. Convert the AI's report text to HTML for PDF generation
    if markdown:
        report_html_body = markdown.markdown(report_text)
    else:
        report_html_body = "<br>\n".join(report_text.split("\n"))

    # Build full HTML with header (logos and title)
    html_content = f"""
    <html>
    <head>
      <title>Matchup Report</title>
      <style> body {{ font-family: Arial, sans-serif; }} </style>
    </head>
    <body>
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; background-color:#333; padding:20px; color:white;">
        <img src="{home_logo}" alt="{home_full} logo" style="width:100px; height:100px; object-fit:contain;">
        <div style="text-align:center; flex-grow:1;">
            <h1 style="font-size:24px; margin:0;">AFPLNA College Football Matchup Report</h1>
            <h2 style="font-size:20px; margin:10px 0 0;">{home_full} vs {away_full} ({year})</h2>
        </div>
        <img src="{away_logo}" alt="{away_full} logo" style="width:100px; height:100px; object-fit:contain;">
      </div>
      <div style="text-align:left;">{report_html_body}</div>
    </body>
    </html>
    """

    # 12. Generate PDF from HTML
    try:
        import pdfkit
    except ImportError:
        return jsonify({"error": "PDF generation library not installed on server."}), 500

    pdfkit_config = None
    wkhtml_path = os.getenv('WKHTMLTOPDF_PATH')  # if wkhtmltopdf is in a custom location
    if wkhtml_path:
        pdfkit_config = pdfkit.configuration(wkhtmltopdf=wkhtml_path)
    try:
        pdfkit.from_string(html_content, filepath, configuration=pdfkit_config)
    except Exception as e:
        logging.error(f"PDF generation failed: {e}")
        return jsonify({"error": "PDF generation failed", "detail": str(e)}), 500

    return jsonify({"message": "Report generated successfully", "filename": filename}), 200

@app.route('/get-report', methods=['GET'])
def get_report():
    api_key_param = request.args.get('api_key')
    if API_KEY_ENV and api_key_param != API_KEY_ENV:
        return jsonify({"error": "Unauthorized"}), 401
    home_short = request.args.get('home_team')
    away_short = request.args.get('away_team')
    if not home_short or not away_short:
        return jsonify({"error": "Missing team name parameters"}), 400
    # Look for today's report file
    today = datetime.now()
    date_str = today.strftime("%B %-d, %Y") if '//' not in '//' else today.strftime("%B %d, %Y").replace(" 0", " ")
    filename = f"{home_short}_{away_short}_{date_str}.pdf"
    filepath = os.path.join(REPORTS_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "Report not found. Please generate it first."}), 404
    # Send the PDF file
    return send_file(filepath, mimetype='application/pdf', as_attachment=True, download_name=filename)

if __name__ == "__main__":
    # Run the Flask development server (for production, use a WSGI server like Gunicorn)
    app.run(host="0.0.0.0", port=5000)
