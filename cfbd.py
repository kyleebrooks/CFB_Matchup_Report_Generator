"""CollegeFootballData (CFBD) access layer.

Every hard statistic in the report comes from here and only from here — the LLM research
calls never touch stats. Endpoints are fetched concurrently, normalized across CFBD's
camelCase/snake_case drift, and pruned before they reach the report model.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

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


CFBD_RETRIES = 2


def pick(d, *keys, default=None):
    """Read the first present key. CFBD mixes camelCase and snake_case across versions."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _get(api_key: str, endpoint: str, params: dict, label: str, errors: list | None = None):
    """GET one CFBD endpoint. Never raises — failures are recorded in `errors`.

    Recording rather than raising matters: a single Patreon-tier endpoint 403ing should
    not sink the whole report, but a 401 on *every* call means the key is bad and the
    caller needs to say so instead of silently emitting a report full of empty sections.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    last = {"status": None, "body": ""}

    for attempt in range(CFBD_RETRIES + 1):
        try:
            resp = requests.get(
                config.CFBD_BASE_URL + endpoint,
                headers=headers,
                params=params,
                timeout=config.CFBD_TIMEOUT,
            )
        except Exception as e:
            last = {"status": None, "body": str(e)[:200]}
            logging.warning(f"CFBD {label} transport error: {e}")
            if attempt < CFBD_RETRIES:
                time.sleep(2 ** attempt)
                continue
            break

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                last = {"status": 200, "body": f"non-JSON response: {resp.text[:150]}"}
                logging.warning(f"CFBD {label} returned non-JSON")
                break

        last = {"status": resp.status_code, "body": resp.text[:200]}
        # 429 is a burst problem, not a credentials problem — back off and retry.
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < CFBD_RETRIES:
            wait = 2 ** attempt
            logging.warning(f"CFBD {label} HTTP {resp.status_code}; retrying in {wait}s")
            time.sleep(wait)
            continue

        logging.warning(f"CFBD {label} HTTP {resp.status_code}: {last['body']}")
        break

    if errors is not None:
        errors.append({"label": label, "endpoint": endpoint,
                       "status": last["status"], "body": last["body"]})
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
    jobs["league::fpi"] = ("/ratings/fpi", {"year": year}, "League FPI Ratings")
    jobs["league::elo"] = ("/ratings/elo", {"year": year}, "League Elo Ratings")
    jobs["meta::teams"] = ("/teams/fbs", {"year": year}, "FBS Teams")
    jobs["meta::talent"] = ("/talent", {"year": year}, "Team Talent")
    jobs["games::A"] = ("/games", {"year": year, "team": home_short}, f"Games ({home_short})")
    jobs["games::B"] = ("/games", {"year": year, "team": away_short}, f"Games ({away_short})")
    jobs["lines::A"] = ("/lines", {"year": year, "team": home_short}, f"Betting Lines ({home_short})")

    results: dict[str, object] = {}
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=config.CFBD_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_get, api_key, ep, params, label, errors): key
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

    auth_failures = [e for e in errors if e["status"] in (401, 403)]
    if auth_failures:
        logging.error(
            f"CFBD rejected {len(auth_failures)}/{len(jobs)} requests "
            f"(e.g. HTTP {auth_failures[0]['status']}: {auth_failures[0]['body']})"
        )

    return {
        "errors": errors,
        "auth_failures": auth_failures,
        "total_requests": len(jobs),
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


def fetch_team(api_key: str, year: int, team: str) -> dict:
    """Everything the single-team report needs, for ONE school.

    Same endpoints as the matchup fetch minus the opponent half, plus the full
    schedule (played and upcoming) and the league-wide tables used for percentiles.
    """
    jobs: dict[str, tuple] = {}
    for endpoint, label in PER_TEAM_ENDPOINTS:
        jobs[f"stat::{label}"] = (endpoint, {"year": year, "team": team}, f"{label} ({team})")

    jobs["league::advanced"] = ("/stats/season/advanced", {"year": year}, "League Advanced Stats")
    jobs["league::sp"] = ("/ratings/sp", {"year": year}, "League SP Ratings")
    jobs["league::fpi"] = ("/ratings/fpi", {"year": year}, "League FPI Ratings")
    jobs["league::elo"] = ("/ratings/elo", {"year": year}, "League Elo Ratings")
    jobs["meta::teams"] = ("/teams/fbs", {"year": year}, "FBS Teams")
    jobs["meta::talent"] = ("/talent", {"year": year}, "Team Talent")
    jobs["games"] = ("/games", {"year": year, "team": team}, f"Games ({team})")
    jobs["records"] = ("/records", {"year": year, "team": team}, f"Records ({team})")

    results: dict[str, object] = {}
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=config.CFBD_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_get, api_key, ep, params, label, errors): key
            for key, (ep, params, label) in jobs.items()
        }
        for fut, key in futures.items():
            try:
                results[key] = fut.result()
            except Exception as e:
                logging.warning(f"CFBD job {key} raised: {e}")
                results[key] = []

    stats = {label: results.get(f"stat::{label}") or [] for _ep, label in PER_TEAM_ENDPOINTS}
    talent_all = results.get("meta::talent") or []
    stats["Team Talent"] = [t for t in talent_all if pick(t, "school", "team") == team]

    auth_failures = [e for e in errors if e["status"] in (401, 403)]
    return {
        "errors": errors,
        "auth_failures": auth_failures,
        "total_requests": len(jobs),
        "stats": stats,
        "league": {"advanced": results.get("league::advanced") or [],
                   "sp": results.get("league::sp") or [],
                   "fpi": results.get("league::fpi") or [],
                   "elo": results.get("league::elo") or []},
        "teams": results.get("meta::teams") or [],
        "games": results.get("games") or [],
        "records": results.get("records") or [],
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
                "classification": (pick(t, "classification", default="") or "").lower(),
                "found": True,
            }
    return {"school": school, "logo": "", "color": "", "alt_color": "",
            "conference": "", "mascot": "", "classification": "", "found": False}


# The full college roster — every division, not just FBS. One fetch a day covers
# every FCS opponent's logo and colors; nothing here is per-report.
_ALL_TEAMS_CACHE: dict = {"year": None, "at": 0.0, "rows": []}
_ALL_TEAMS_TTL = 24 * 3600
_ALL_TEAMS_LOCK = threading.Lock()


def all_teams(api_key: str, year: int | None = None,
              errors: list | None = None) -> list:
    """CFBD's complete team list (FBS, FCS, II, III), cached in-process for a day."""
    year = year or season_year(datetime.now())
    with _ALL_TEAMS_LOCK:
        if (_ALL_TEAMS_CACHE["rows"] and _ALL_TEAMS_CACHE["year"] == year
                and time.time() - _ALL_TEAMS_CACHE["at"] < _ALL_TEAMS_TTL):
            return _ALL_TEAMS_CACHE["rows"]
    rows = _get(api_key, "/teams", {"year": year}, "All teams", errors) or []
    if rows:
        with _ALL_TEAMS_LOCK:
            _ALL_TEAMS_CACHE.update(year=year, at=time.time(), rows=rows)
    return rows


def resolve_team_meta(api_key: str, teams: list, school: str,
                      year: int | None = None) -> dict:
    """team_meta, with the full roster as a fallback for non-FBS schools.

    Reports routinely involve an FCS opponent, and the FBS-only list the report
    already fetched knows nothing about them — which is why those reports shipped
    with a blank logo and default chart colors. The fallback costs one cached
    request per day, and only fires when the school is genuinely not FBS.
    """
    meta = team_meta(teams, school)
    if meta["found"]:
        return meta
    try:
        fallback = team_meta(all_teams(api_key, year), school)
    except Exception as e:
        logging.warning(f"Full-roster lookup for {school} failed: {e}")
        return meta
    return fallback if fallback["found"] else meta


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


def check_key(api_key: str, year: int) -> dict:
    """One cheap authenticated call, to distinguish a bad key from an empty season."""
    errors: list[dict] = []
    data = _get(api_key, "/teams/fbs", {"year": year}, "key check", errors)
    if errors:
        e = errors[0]
        return {"ok": False, "status": e["status"], "detail": e["body"]}
    return {"ok": True, "teams": len(data) if isinstance(data, list) else 0}


def probe(api_key: str, year: int, team: str = "Georgia") -> dict:
    """Hit every endpoint the pipeline uses, one at a time, and report each result.

    Sequential on purpose: the report path fires these concurrently, so probing them
    serially separates "this endpoint rejects my key / tier" from "I got rate limited".
    """
    checks = []
    probes = [(ep, {"year": year, "team": team}, label) for ep, label in PER_TEAM_ENDPOINTS]
    probes += [
        ("/teams/fbs", {"year": year}, "FBS Teams"),
        ("/talent", {"year": year}, "Team Talent (league-wide)"),
        ("/stats/season/advanced", {"year": year}, "Advanced Stats (league-wide)"),
        ("/ratings/sp", {"year": year}, "SP Ratings (league-wide)"),
        ("/games", {"year": year, "team": team}, "Games (NEW)"),
        ("/lines", {"year": year, "team": team}, "Betting Lines (NEW)"),
    ]

    for endpoint, params, label in probes:
        errors: list[dict] = []
        data = _get(api_key, endpoint, params, label, errors)
        if errors:
            e = errors[0]
            checks.append({"endpoint": endpoint, "label": label, "ok": False,
                           "status": e["status"], "body": e["body"]})
        else:
            checks.append({"endpoint": endpoint, "label": label, "ok": True,
                           "status": 200, "rows": len(data) if isinstance(data, list) else "object"})
        time.sleep(0.15)  # stay well clear of the rate limiter while probing

    failed = [c for c in checks if not c["ok"]]
    return {
        "ok": not failed,
        "year": year,
        "team": team,
        "passed": len(checks) - len(failed),
        "total": len(checks),
        "failed_endpoints": [c["endpoint"] for c in failed],
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Schedule windows: the games a client can pick from
# ---------------------------------------------------------------------------
def _parse_stamp(stamp):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _game_row(g: dict) -> dict | None:
    row = {
        "id": g.get("id"),
        "season": g.get("season"),
        "week": g.get("week"),
        "season_type": g.get("seasonType"),
        "start": g.get("startDate"),
        "start_time_tbd": bool(g.get("startTimeTBD")),
        "completed": bool(g.get("completed")),
        "neutral_site": bool(g.get("neutralSite")),
        "venue": g.get("venue"),
        "home": g.get("homeTeam"),
        "home_conference": g.get("homeConference"),
        "home_points": g.get("homePoints"),
        "away": g.get("awayTeam"),
        "away_conference": g.get("awayConference"),
        "away_points": g.get("awayPoints"),
    }
    if not row["id"] or not row["home"] or not row["away"]:
        return None
    return row


def calendar_weeks(api_key: str, year: int, errors: list | None = None) -> list[dict]:
    """The season's weeks, parsed, sorted, JSON-ready."""
    raw = _get(api_key, "/calendar", {"year": year}, "Calendar", errors) or []
    weeks = []
    for w in raw:
        start, end = _parse_stamp(w.get("startDate")), _parse_stamp(w.get("endDate"))
        if not start or not end:
            continue
        weeks.append({"week": w.get("week"), "season_type": w.get("seasonType", "regular"),
                      "start": w.get("startDate"), "end": w.get("endDate"),
                      "_start": start, "_end": end})
    weeks.sort(key=lambda w: w["_start"])
    return weeks


def _strip_weeks(weeks: list[dict]) -> list[dict]:
    return [{k: v for k, v in w.items() if not k.startswith("_")} for w in weeks]


def games_for(api_key: str, year: int, week: int, season_type: str,
              errors: list | None = None) -> list[dict]:
    rows = _get(api_key, "/games",
                {"year": year, "week": week, "seasonType": season_type or "regular",
                 "classification": "fbs"},
                f"Games ({year} {season_type} week {week})", errors) or []
    out = [r for r in (_game_row(g) for g in rows) if r]
    out.sort(key=lambda r: (r["start"] or "", r["home"]))
    return out


def _week_around(weeks: list[dict], now) -> int | None:
    """Index of the week containing `now`, else the next upcoming, else the last."""
    if not weeks:
        return None
    for i, w in enumerate(weeks):
        if w["_start"] <= now <= w["_end"]:
            return i
    for i, w in enumerate(weeks):
        if w["_start"] > now:
            return i
    return len(weeks) - 1


def schedule_windows(api_key: str, now=None) -> dict:
    """The default picker feed: upcoming games and recent finals around today.

    The calendar names the week that contains `now`; the pickable windows are that
    week plus the next (upcoming) and the last two weeks (recent finals). Off-season,
    when no week contains today, the nearest week is used — so in August the selector
    shows week 1, and in February the postseason. The season's full week list and the
    current week ride along so a client can build year/week selectors.
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    year = season_year(now)
    errors: list[dict] = []
    weeks = calendar_weeks(api_key, year, errors)
    current = _week_around(weeks, now)

    upcoming: list[dict] = []
    recent: list[dict] = []
    if current is not None:
        seen: set = set()
        for w in weeks[max(0, current - 2):current + 2]:
            key = (w["season_type"], w["week"])
            if key in seen:
                continue
            seen.add(key)
            for row in games_for(api_key, year, w["week"], w["season_type"], errors):
                started = _parse_stamp(row["start"])
                if row["completed"]:
                    recent.append(row)
                elif started is None or started >= now:
                    upcoming.append(row)
        upcoming.sort(key=lambda r: (r["start"] or "", r["home"]))
        recent.sort(key=lambda r: (r["start"] or "", r["home"]), reverse=True)

    return {
        "season": year,
        "weeks": _strip_weeks(weeks),
        "current": ({"week": weeks[current]["week"],
                     "season_type": weeks[current]["season_type"]}
                    if current is not None else None),
        "selected": None,
        "upcoming": upcoming,
        "recent": recent,
        "errors": errors,
    }


def week_games(api_key: str, year: int, week: int | None = None,
               season_type: str | None = None, now=None) -> dict:
    """One explicit season/week, split into upcoming games and finals.

    Prior seasons are entirely finals; future weeks entirely upcoming. When no week is
    named, the current season defaults to the week around today and any other season
    to its first week.
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    errors: list[dict] = []
    weeks = calendar_weeks(api_key, year, errors)

    if week is None:
        idx = _week_around(weeks, now) if year == season_year(now) else 0
        if idx is not None and weeks:
            week = weeks[idx]["week"]
            season_type = season_type or weeks[idx]["season_type"]
        else:
            week = 1
    if not season_type:
        match = next((w for w in weeks if w["week"] == week), None)
        season_type = match["season_type"] if match else "regular"

    rows = games_for(api_key, year, week, season_type, errors)
    upcoming = [r for r in rows if not r["completed"]]
    recent = sorted((r for r in rows if r["completed"]),
                    key=lambda r: (r["start"] or "", r["home"]), reverse=True)

    current = _week_around(weeks, now) if year == season_year(now) else None
    return {
        "season": year,
        "weeks": _strip_weeks(weeks),
        "current": ({"week": weeks[current]["week"],
                     "season_type": weeks[current]["season_type"]}
                    if current is not None else None),
        "selected": {"week": week, "season_type": season_type},
        "upcoming": upcoming,
        "recent": recent,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# One finished game, end to end — the Full Game Recap's entire data diet
# ---------------------------------------------------------------------------
def fetch_game_recap(api_key: str, game_id: int) -> dict:
    """Everything CFBD knows about one game: the game row, the advanced box score,
    every drive, every play, and the player-play stat lines.

    Drives and plays cannot be filtered by game id, so the game row is fetched first
    and its year/week/team scope the follow-up calls; rows for other games in the
    same week are dropped by gameId afterwards.
    """
    errors: list[dict] = []

    # /games requires year "except when id is specified".
    games = _get(api_key, "/games", {"id": game_id}, "Game", errors)
    game = _first(games)
    if not game:
        return {"game": {}, "errors": errors, "auth_failures":
                [e for e in errors if e["status"] in (401, 403)], "total_requests": 1}

    year = game.get("season")
    week = game.get("week")
    season_type = game.get("seasonType", "regular")
    home, away = game.get("homeTeam"), game.get("awayTeam")

    jobs = {
        "box": ("/game/box/advanced", {"id": game_id}, "Advanced Box Score"),
        "drives": ("/drives", {"year": year, "week": week, "seasonType": season_type,
                               "team": home}, "Drives"),
        "plays": ("/plays", {"year": year, "week": week, "seasonType": season_type,
                             "team": home}, "Plays"),
        "play_stats": ("/plays/stats", {"gameId": game_id}, "Player-Play Stats"),
        "teams": ("/teams/fbs", {"year": year}, "FBS Teams"),
    }

    results: dict = {}
    with ThreadPoolExecutor(max_workers=min(5, config.CFBD_MAX_WORKERS)) as pool:
        futures = {pool.submit(_get, api_key, ep, params, label, errors): key
                   for key, (ep, params, label) in jobs.items()}
        for fut, key in futures.items():
            try:
                results[key] = fut.result()
            except Exception as e:
                logging.warning(f"CFBD recap job {key} raised: {e}")
                results[key] = []

    drives = [d for d in (results.get("drives") or [])
              if d.get("gameId") in (game_id, None)]
    plays = [p for p in (results.get("plays") or []) if p.get("gameId") == game_id]

    box = results.get("box")
    if isinstance(box, list):          # tolerate either envelope shape
        box = _first(box)

    return {
        "game": game,
        "box": box or {},
        "drives": drives,
        "plays": plays,
        "play_stats": results.get("play_stats") or [],
        "teams": results.get("teams") or [],
        "errors": errors,
        "auth_failures": [e for e in errors if e["status"] in (401, 403)],
        "total_requests": len(jobs) + 1,
    }
