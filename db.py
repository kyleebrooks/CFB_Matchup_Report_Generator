"""Database helpers: the GoDaddy MySQL key store and the local SQLite Rotowire feed."""

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta

import pymysql

import config


def format_friendly_date(dt: datetime) -> str:
    """Return 'Month D, YYYY' without zero-padding the day, cross-platform."""
    try:
        return dt.strftime("%B %-d, %Y")
    except Exception:
        return dt.strftime("%B %d, %Y").replace(" 0", " ")


def get_db_connection():
    """Return a MySQL connection configured for long-running operations.

    We DO NOT keep connections open while doing network calls; open only when needed.
    """
    conn = pymysql.connect(
        host=config.DB_HOST,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
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
    """Fetch an API key from the API_KEYS table; returns stripped string or None."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT `KEY` FROM API_KEYS WHERE API_NAME=%s LIMIT 1", (name,))
            row = cur.fetchone()
            if not row:
                return None
            key = row[0]
            if key and str(key).strip():
                return str(key).strip()
            return None
    finally:
        conn.close()


def resolve_openrouter_key() -> str | None:
    """OpenRouter is the only LLM provider. Prefer the API_KEYS table, fall back to env.

    Row expected in MySQL:
        INSERT INTO API_KEYS (`API_NAME`, `KEY`) VALUES ('openrouter', 'sk-or-v1-...');
    """
    for name in ('openrouter', 'OPENROUTER', 'open_router'):
        try:
            key = get_api_key(name)
        except Exception as e:
            logging.warning(f"API_KEYS lookup for '{name}' failed: {e}")
            key = None
        if key:
            return key
    return os.getenv('OPENROUTER_API_KEY')


def resolve_cfbd_key() -> str | None:
    for name in ('CFD', 'CFBD'):
        try:
            key = get_api_key(name)
        except Exception as e:
            logging.warning(f"API_KEYS lookup for '{name}' failed: {e}")
            key = None
        if key:
            return key
    return os.getenv('CFBD_API_KEY')


# ---------------------------------------------------------------------------
# Rotowire (local SQLite)
# ---------------------------------------------------------------------------
def get_rotowire_db_connection():
    """Return a connection to the local SQLite Rotowire database.

    Ensures the table structure exists.
    """
    db_dir = os.path.dirname(config.ROTOWIRE_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(config.ROTOWIRE_DB_PATH)
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
        # The scrape's duplicate check compares every column, which was a full table
        # scan per incoming row. Additive and cheap; nothing depends on it being there.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rotowire_lookup "
            "ON rotowire (player_name, headline)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rotowire_date ON rotowire (date_text)"
        )
    return conn


_STOP_WORDS = {
    'university', 'the', 'of', 'state', 'college', 'football', 'a', 'and',
}


def _normalize_team(name: str) -> str:
    return re.sub(r'[^a-z0-9 ]+', '', (name or '').lower()).strip()


def _team_tokens(name: str) -> set[str]:
    return {t for t in _normalize_team(name).split() if t and t not in _STOP_WORDS}


def team_matches(row_team: str, short_name: str, full_name: str) -> bool:
    """Tolerant match between a Rotowire team string and our two naming variants.

    Rotowire writes team names inconsistently ("Georgia", "Georgia Bulldogs", "UGA"),
    so we compare on normalized substrings and on distinctive token overlap.
    """
    row_n = _normalize_team(row_team)
    if not row_n:
        return False
    short_n = _normalize_team(short_name)
    full_n = _normalize_team(full_name)

    if row_n in (short_n, full_n):
        return True
    if short_n and (short_n in row_n or row_n in short_n):
        return True
    if full_n and (full_n in row_n or row_n in full_n):
        return True

    # Fall back to distinctive-token overlap ("Miami (FL)" vs "Miami Hurricanes").
    row_tokens = _team_tokens(row_team)
    if not row_tokens:
        return False
    return bool(row_tokens & (_team_tokens(short_name) | _team_tokens(full_name)))


def fetch_rotowire_for_team(short_name: str, full_name: str, days: int | None = None) -> list[dict]:
    """Return recent Rotowire items for ONE team.

    The previous implementation handed the model every Rotowire row from the last week
    regardless of team and asked it to filter. Filtering here keeps unrelated teams out
    of the prompt entirely.
    """
    days = days or config.ROTOWIRE_WINDOW_DAYS
    dates = [format_friendly_date(datetime.now() - timedelta(days=i)) for i in range(days)]
    cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()

    conn = get_rotowire_db_connection()
    items: list[dict] = []
    try:
        cur = conn.cursor()
        # Rows written by the collector carry an ISO news_date and are selected by range.
        # Legacy rows have none, so they keep the original exact-string match — which is
        # why a change in the old scraper's date format could empty the feed silently.
        has_news_date = any(r[1] == "news_date"
                            for r in cur.execute("PRAGMA table_info(rotowire)"))
        placeholders = ",".join(["?"] * len(dates))
        columns = ("player_name, headline, team_name, date_text, news_text, position, "
                   "analysis_text")
        if has_news_date:
            cur.execute(
                f"SELECT {columns}, source_name, source_url, status FROM rotowire "
                f"WHERE (news_date IS NOT NULL AND news_date >= ?) "
                f"   OR (news_date IS NULL AND date_text IN ({placeholders}))",
                [cutoff] + dates,
            )
        else:
            cur.execute(
                f"SELECT {columns} FROM rotowire WHERE date_text IN ({placeholders})",
                dates,
            )
        for row in cur.fetchall() or []:
            if not team_matches(row["team_name"], short_name, full_name):
                continue
            keys = row.keys()
            items.append({
                "team": row["team_name"],
                "player": row["player_name"],
                "position": row["position"],
                "date": row["date_text"],
                "headline": row["headline"],
                "news": row["news_text"],
                "analysis": row["analysis_text"],
                "status": row["status"] if "status" in keys else "",
                "source_name": (row["source_name"] if "source_name" in keys else "")
                               or "College football injury feed",
                "source_url": (row["source_url"] if "source_url" in keys else "")
                              or "https://www.rotowire.com/cfootball/news.php",
            })
        cur.close()
    finally:
        conn.close()
    return items
