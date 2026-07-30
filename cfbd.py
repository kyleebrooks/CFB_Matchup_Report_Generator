"""CollegeFootballData (CFBD) access layer.

Every hard statistic in the report comes from here and only from here — the LLM research
calls never touch stats. Endpoints are fetched concurrently, normalized across CFBD's
camelCase/snake_case drift, and pruned before they reach the report model.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import requests

import config

# (endpoint, human label). Fetched per team.
PER_TEAM_ENDPOINTS = [
    ("/ratings/sp",             "SP Ratings"),
    ("/ratings/elo",            "ELO Ratings"),
    ("/ratings/fpi",            "FPI Ratings"),
    ("/stats/season/advanced",  "Advanced Team Stats"),
    ("/player/returning",       "Returning Production"),
    ("/ppa/games",              "Team PPA"),
    ("/ppa/players/season",     "Player PPA"),
    ("/stats/season",           "Team Season Stats"),
    ("/wepa/team/season",       "Adjusted Team Metrics"),
]


def pick(d, *keys, default=None):
    """Read the first present key. CFBD mixes camelCase and snake_case across versions."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _get(api_key: str, endpoint: str, params: dict, label: str):
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(
            config.CFBD_BASE_URL + endpoint,
            headers=headers,
            params=params,
            timeout=config.CFBD_TIMEOUT,
        )
        if resp.status_code != 200:
            logging.warning(f"CFBD {label} HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        return resp.json()
    except Exception as e:
        logging.warning(f"CFBD {label} failed: {e}")
        return []


def _first(rows):
    return rows[0] if isinstance(rows, list) and rows else {}


def season_year(now) -> int:
    """CFB seasons straddle the new year — January bowl/playoff games belong to year-1."""
    return now.year if now.month >= 3 else now.year - 1


def fetch_all(api_key: str, year: int, home_short: str, away_short: str) -> dict:
    """Fetch every CFBD payload the report needs, concurrently.

    Returns a dict with:
      stats   -> {label: {"teamA": [...], "teamB": [...]}}
      league  -> league-wide advanced stats + SP ratings (for percentile context)
      teams   -> FBS team metadata (logos, colors)
      talent  -> league-wide talent table (/talent ignores a team filter)
      games   -> {"teamA": [...], "teamB": [...]} completed + scheduled games
      lines   -> betting lines for the two teams
    """
    jobs: dict[str, tuple] = {}
    for endpoint, label in PER_TEAM_ENDPOINTS:
        jobs[f"stat::{label}::A"] = (endpoint, {"year": year, "team": home_short}, f"{label} ({home_short})")
        jobs[f"stat::{label}::B"] = (endpoint, {"year": year, "team": away_short}, f"{label} ({away_short})")

    # League-wide pulls power the percentile scaling used by the radar/mismatch charts.
    jobs["league::advanced"] = ("/stats/season/advanced", {"year": year}, "League Advanced Stats")
    jobs["league::sp"] = ("/ratings/sp", {"year": year}, "League SP Ratings")
    jobs["meta::teams"] = ("/teams/fbs", {"year": year}, "FBS Teams")
    jobs["meta::talent"] = ("/talent", {"year": year}, "Team Talent")
    jobs["games::A"] = ("/games", {"year": year, "team": home_short}, f"Games ({home_short})")
    jobs["games::B"] = ("/games", {"year": year, "team": away_short}, f"Games ({away_short})")
    jobs["lines::A"] = ("/lines", {"year": year, "team": home_short}, f"Betting Lines ({home_short})")

    results: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=config.CFBD_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_get, api_key, ep, params, label): key
            for key, (ep, params, label) in jobs.items()
        }
        for fut, key in futures.items():
            try:
                results[key] = fut.result()
            except Exception as e:
                logging.warning(f"CFBD job {key} raised: {e}")
                results[key] = []

    stats: dict[str, dict] = {}
    for _endpoint, label in PER_TEAM_ENDPOINTS:
        stats[label] = {
            "teamA": results.get(f"stat::{label}::A") or [],
            "teamB": results.get(f"stat::{label}::B") or [],
        }

    # /talent has no team filter, so slice the league table down to our two schools.
    talent_all = results.get("meta::talent") or []
    stats["Team Talent"] = {
        "teamA": [t for t in talent_all if pick(t, "school", "team") == home_short],
        "teamB": [t for t in talent_all if pick(t, "school", "team") == away_short],
    }

    return {
        "stats": stats,
        "league": {
            "advanced": results.get("league::advanced") or [],
            "sp": results.get("league::sp") or [],
            "talent": talent_all,
        },
        "teams": results.get("meta::teams") or [],
        "games": {
            "teamA": results.get("games::A") or [],
            "teamB": results.get("games::B") or [],
        },
        "lines": results.get("lines::A") or [],
    }


# ---------------------------------------------------------------------------
# Team metadata (logo + official colors, used to style every chart)
# ---------------------------------------------------------------------------
def team_meta(teams: list, school: str) -> dict:
    for t in teams or []:
        if pick(t, "school", "team") == school:
            logos = t.get("logos") or []
            return {
                "school": school,
                "logo": logos[0] if logos else "",
                "color": (pick(t, "color") or "").strip(),
                "alt_color": (pick(t, "alternateColor", "alt_color") or "").strip(),
                "conference": pick(t, "conference", default=""),
                "mascot": pick(t, "mascot", default=""),
            }
    return {"school": school, "logo": "", "color": "", "alt_color": "", "conference": "", "mascot": ""}


# ---------------------------------------------------------------------------
# Game results — season form, points for/against
# ---------------------------------------------------------------------------
def normalize_games(games: list, school: str) -> list[dict]:
    """Flatten CFBD /games rows into this-team-centric records."""
    out = []
    for g in games or []:
        home = pick(g, "homeTeam", "home_team", default="")
        away = pick(g, "awayTeam", "away_team", default="")
        hp = pick(g, "homePoints", "home_points")
        ap = pick(g, "awayPoints", "away_points")
        completed = pick(g, "completed", default=None)
        if completed is None:
            completed = hp is not None and ap is not None
        is_home = home == school
        opponent = away if is_home else home
        pf = hp if is_home else ap
        pa = ap if is_home else hp
        out.append({
            "week": pick(g, "week"),
            "season_type": pick(g, "seasonType", "season_type", default="regular"),
            "start_date": pick(g, "startDate", "start_date", default=""),
            "opponent": opponent,
            "home": is_home,
            "neutral": bool(pick(g, "neutralSite", "neutral_site", default=False)),
            "points_for": pf,
            "points_against": pa,
            "completed": bool(completed),
        })
    out.sort(key=lambda r: (r["week"] is None, r["week"] or 0))
    return out


def scoring_profile(games: list[dict]) -> dict:
    """Average points scored / allowed over completed games."""
    played = [g for g in games if g["completed"] and g["points_for"] is not None and g["points_against"] is not None]
    if not played:
        return {"games": 0, "ppg": None, "papg": None, "wins": 0, "losses": 0}
    pf = sum(g["points_for"] for g in played)
    pa = sum(g["points_against"] for g in played)
    wins = sum(1 for g in played if g["points_for"] > g["points_against"])
    return {
        "games": len(played),
        "ppg": round(pf / len(played), 2),
        "papg": round(pa / len(played), 2),
        "wins": wins,
        "losses": len(played) - wins,
    }


def find_matchup_line(lines: list, home_short: str, away_short: str) -> dict | None:
    """Locate the upcoming game between our two teams and average the books' numbers.

    CFBD reports spread from the HOME team's perspective (negative = home favored); we
    convert to a positive-means-home-favored margin to match every other number in the
    report.
    """
    for g in lines or []:
        gh = pick(g, "homeTeam", "home_team", default="")
        ga = pick(g, "awayTeam", "away_team", default="")
        if {gh, ga} != {home_short, away_short}:
            continue

        spreads, totals, providers = [], [], []
        for ln in g.get("lines") or []:
            sp = pick(ln, "spread")
            ou = pick(ln, "overUnder", "over_under")
            try:
                if sp is not None:
                    spreads.append(float(sp))
            except (TypeError, ValueError):
                pass
            try:
                if ou is not None:
                    totals.append(float(ou))
            except (TypeError, ValueError):
                pass
            prov = pick(ln, "provider")
            if prov:
                providers.append(prov)

        if not spreads and not totals:
            continue

        avg_spread = sum(spreads) / len(spreads) if spreads else None
        avg_total = sum(totals) / len(totals) if totals else None
        # CFBD spread is relative to its own home team; flip if that isn't our home team.
        margin = None
        if avg_spread is not None:
            margin = -avg_spread if gh == home_short else avg_spread
        return {
            "week": pick(g, "week"),
            "home_team": gh,
            "away_team": ga,
            "providers": sorted(set(providers)),
            "market_margin_home": round(margin, 2) if margin is not None else None,
            "market_total": round(avg_total, 2) if avg_total is not None else None,
        }
    return None


# ---------------------------------------------------------------------------
# Percentile context — turns a bare number into "83rd percentile nationally"
# ---------------------------------------------------------------------------
ADVANCED_METRICS = [
    # (label, side, path, higher_is_better)
    ("Off. PPA/play",        "offense", ("ppa",),                 True),
    ("Off. Success Rate",    "offense", ("successRate",),         True),
    ("Off. Explosiveness",   "offense", ("explosiveness",),       True),
    ("Off. Line Yards",      "offense", ("lineYards",),           True),
    ("Off. Pts/Opportunity", "offense", ("pointsPerOpportunity",), True),
    ("Def. PPA/play",        "defense", ("ppa",),                 False),
    ("Def. Success Rate",    "defense", ("successRate",),         False),
    ("Def. Explosiveness",   "defense", ("explosiveness",),       False),
    ("Def. Line Yards",      "defense", ("lineYards",),           False),
    ("Def. Havoc",           "defense", ("havoc", "total"),       True),
]


def _dig(row: dict, side: str, path: tuple):
    node = row.get(side) if isinstance(row, dict) else None
    for key in path:
        if not isinstance(node, dict):
            return None
        node = pick(node, key)
    try:
        return float(node)
    except (TypeError, ValueError):
        return None


def percentile_of(value: float, population: list[float], higher_is_better: bool) -> float | None:
    """Percentile rank of `value` within `population`, oriented so 100 is always good."""
    pop = [p for p in population if p is not None]
    if value is None or len(pop) < 5:
        return None
    below = sum(1 for p in pop if p < value)
    pct = 100.0 * below / len(pop)
    return round(pct if higher_is_better else 100.0 - pct, 1)


def build_percentiles(league_advanced: list, home_row: dict, away_row: dict) -> dict:
    """{metric_label: {"home": pct, "away": pct, "home_raw":…, "away_raw":…}}"""
    out: dict[str, dict] = {}
    for label, side, path, higher in ADVANCED_METRICS:
        population = [_dig(r, side, path) for r in league_advanced or []]
        hv = _dig(home_row, side, path)
        av = _dig(away_row, side, path)
        out[label] = {
            "home_raw": hv,
            "away_raw": av,
            "home": percentile_of(hv, population, higher),
            "away": percentile_of(av, population, higher),
            "higher_is_better": higher,
        }
    return out


# ---------------------------------------------------------------------------
# Pruning — keep the report prompt focused (and the token bill sane)
# ---------------------------------------------------------------------------
def _player_ppa_total(p: dict) -> float:
    total = p.get("totalPPA")
    if isinstance(total, dict):
        try:
            return abs(float(pick(total, "all", default=0) or 0))
        except (TypeError, ValueError):
            return 0.0
    try:
        return abs(float(total or 0))
    except (TypeError, ValueError):
        return 0.0


def prune_player_ppa(rows: list, limit: int | None = None) -> list:
    limit = limit or config.TOP_PLAYERS_PER_TEAM
    if not isinstance(rows, list):
        return []
    return sorted(rows, key=_player_ppa_total, reverse=True)[:limit]


def prune_for_prompt(stats: dict) -> dict:
    """Copy of the stats bundle with the heaviest payload (player PPA) trimmed to top-N."""
    pruned = dict(stats)
    player_ppa = stats.get("Player PPA") or {}
    pruned["Player PPA"] = {
        "teamA": prune_player_ppa(player_ppa.get("teamA")),
        "teamB": prune_player_ppa(player_ppa.get("teamB")),
        "_note": f"Top {config.TOP_PLAYERS_PER_TEAM} players per team by absolute total PPA.",
    }
    return pruned
