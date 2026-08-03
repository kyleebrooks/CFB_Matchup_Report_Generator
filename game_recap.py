"""Full Game Recap: what actually happened in one finished game, and why.

Unlike the matchup and team reports, this type runs NO live web research. Its entire
diet is CollegeFootballData: the game row, the advanced box score, every drive, every
play, and the player-play stat lines. The model's job is synthesis and judgement —
what went right, what went wrong, what was adjusted and what was not — with every
claim anchored to the supplied data.
"""

import json
import logging
import os
import time
from datetime import datetime

import accounts
import cfbd
import charts as charts_mod
import config
import db
import render
import report as report_mod
import research
from pipeline import PipelineError

STAGES = {
    "start":  (5,   "Starting up"),
    "gather": (15,  "Pulling the game, drives, plays and box score from CFBD"),
    "charts": (55,  "Rendering charts"),
    "write":  (65,  "Writing the recap"),
    "pdf":    (92,  "Building the PDF"),
    "done":   (100, "Complete"),
}

# The repeatable skeleton. Every recap has exactly these sections in this order, so
# recaps read the same way week after week and can be compared side by side.
SECTIONS = [
    ("Final Score and Game Story", (
        "The result, stated immediately: final score, where and when, and the one-"
        "paragraph story of the game — was it controlled throughout, a comeback, a "
        "collapse, decided late? Use the excitement index and win-probability swing "
        "to characterise it honestly."
    )),
    ("How the Game Unfolded", (
        "Quarter by quarter, from the line scores, drives and plays: who scored when, "
        "how momentum moved, and what each stretch of the game looked like. Anchor "
        "every scoring event to its drive (length, plays, time)."
    )),
    ("Key Drives and Turning Points", (
        "The three to five possessions that decided the game: long scoring marches, "
        "empty trips into scoring position, turnovers, fourth-down decisions, special-"
        "teams swings. For each: the situation, what happened, and what it cost or "
        "bought in points."
    )),
    ("What Went Right", (
        "For EACH team in turn, what genuinely worked, tied to the numbers: efficiency "
        "edges, individual performances, situational execution. Separate the two teams "
        "with bolded lead-ins."
    )),
    ("What Went Wrong", (
        "For EACH team in turn, what failed, tied to the numbers: wasted scoring "
        "opportunities, efficiency deficits, negative plays, situational failures. "
        "Bolded lead-ins again. Be specific about cost in points."
    )),
    ("Adjustments and Game Management", (
        "Compare each team's first half to its second half using the half-split data: "
        "what changed in approach or effectiveness, what visibly was NOT adjusted, and "
        "the game-management decisions the data exposes — fourth downs, red-zone "
        "choices, tempo. Where the data shows a team kept failing the same way, say "
        "so plainly."
    )),
    ("What Could Have Been Done Differently", (
        "Grounded counterfactuals only — each one anchored to something in the data: "
        "points left on the field in scoring opportunities, field-position surrendered, "
        "personnel usage the impact numbers argue against. No speculation about "
        "injuries, play-calling intent, or anything the data cannot see."
    )),
    ("Top Performers", (
        "The players who actually moved the game, from the box-score PPA and the "
        "player stat lines: their production, and where in the game it came."
    )),
    ("Play-Type Breakdown", (
        "What each team ran and how well it worked, from the play-type data: success "
        "rate, yards per play, explosives and stuffs by play type — the OFFENSIVE view "
        "for each team, then the DEFENSIVE view (what each defense allowed, by type). "
        "The success definition is supplied in the data; state it once. Then the "
        "players executing: from the player-execution data, which ball-carriers, "
        "passers and targets drove each play type's success rate up or down, and which "
        "defenders kept showing up in the plays that failed. Use tables for the "
        "type-level numbers; name names in the prose."
    )),
    ("Advanced Box Score Analysis", (
        "The full statistical autopsy: efficiency, explosiveness, field position, "
        "havoc, scoring opportunities, rushing and passing splits. Present the "
        "comparisons as a table where that is clearer than prose."
    )),
    ("Final Assessment", (
        "What this result actually says about each team going forward — strictly what "
        "the on-field evidence supports, no season projections beyond it."
    )),
]

SYSTEM_PROMPT = (
    "You are a college football analyst writing a post-game recap from verified game "
    "data. You write with authority and specificity: every claim is anchored to the "
    "supplied numbers, and you never invent events, quotes, injuries or context that "
    "is not in the data. Plain, confident, readable prose — no hedging filler."
)


def _clock(c: dict | None) -> str:
    c = c or {}
    return f"{int(c.get('minutes') or 0)}:{int(c.get('seconds') or 0):02d}"


def _compact_drives(drives: list) -> list[dict]:
    out = []
    for d in drives:
        out.append({
            "n": d.get("driveNumber"),
            "offense": d.get("offense"),
            "start_period": d.get("startPeriod"),
            "start_yards_to_goal": d.get("startYardsToGoal"),
            "plays": d.get("plays"),
            "yards": d.get("yards"),
            "elapsed": _clock(d.get("elapsed")),
            "result": d.get("driveResult"),
            "scoring": bool(d.get("scoring")),
            "end_score": f"{d.get('endOffenseScore')}-{d.get('endDefenseScore')}",
        })
    return out


def _notable_plays(plays: list, limit: int = 110) -> list[dict]:
    """The plays worth narrating: scores, chunk gains, turnovers, fourth downs."""
    def notable(p):
        text = (p.get("playType") or "").lower()
        return (bool(p.get("scoring"))
                or (p.get("yardsGained") or 0) >= 20
                or (p.get("yardsGained") or 0) <= -8
                or "interception" in text or "fumble" in text
                or "punt block" in text or "missed" in text
                or int(p.get("down") or 0) == 4)

    picked = [p for p in plays if notable(p)]
    picked.sort(key=lambda p: (int(p.get("period") or 0),
                               -(int((p.get("clock") or {}).get("minutes") or 0) * 60
                                 + int((p.get("clock") or {}).get("seconds") or 0))))
    out = []
    for p in picked[:limit]:
        out.append({
            "q": p.get("period"),
            "clock": _clock(p.get("clock")),
            "offense": p.get("offense"),
            "down_distance": f"{p.get('down')} & {p.get('distance')}",
            "yards_to_goal": p.get("yardsToGoal"),
            "type": p.get("playType"),
            "yards": p.get("yardsGained"),
            "scoring": bool(p.get("scoring")),
            "text": (p.get("playText") or "")[:220],
            "score": f"{p.get('offenseScore')}-{p.get('defenseScore')}",
        })
    return out


def _half_splits(plays: list, home: str, away: str) -> dict:
    """First-half vs second-half production per team — the adjustments evidence."""
    def side(team, periods):
        rows = [p for p in plays if p.get("offense") == team
                and int(p.get("period") or 0) in periods
                and p.get("yardsGained") is not None]
        scrimmage = [p for p in rows
                     if (p.get("playType") or "").lower() not in
                     ("kickoff", "punt", "timeout", "end period", "end of half")]
        if not scrimmage:
            return {"plays": 0}
        yards = sum(int(p.get("yardsGained") or 0) for p in scrimmage)
        chunk = sum(1 for p in scrimmage if (p.get("yardsGained") or 0) >= 15)
        stuff = sum(1 for p in scrimmage if (p.get("yardsGained") or 0) <= 0)
        return {"plays": len(scrimmage), "yards": yards,
                "yards_per_play": round(yards / len(scrimmage), 2),
                "chunk_plays_15plus": chunk, "zero_or_negative": stuff}

    return {team: {"first_half": side(team, (1, 2)),
                   "second_half": side(team, (3, 4, 5))}
            for team in (home, away)}


# ---------------------------------------------------------------------------
# Play-type analytics
# ---------------------------------------------------------------------------
# The standard down-and-distance success definition used across analytics sites.
SUCCESS_DEFINITION = (
    "A play is a success when it gains 50% of the yards to go on 1st down, 70% on "
    "2nd down, or 100% on 3rd/4th down; offensive touchdowns always count, turnovers "
    "never do. Special-teams and administrative plays are excluded."
)

_NON_SCRIMMAGE = ("kickoff", "punt", "field goal", "timeout", "end period",
                  "end of half", "end of game", "penalty", "extra point",
                  "two point", "defensive 2pt")


def _is_scrimmage(play: dict) -> bool:
    text = (play.get("playType") or "").lower()
    if not text or any(marker in text for marker in _NON_SCRIMMAGE):
        return False
    return play.get("yardsGained") is not None


def _is_turnover(play: dict) -> bool:
    text = (play.get("playType") or "").lower()
    return "interception" in text or "fumble recovery (opponent)" in text \
        or "fumble return" in text


def _is_success(play: dict) -> bool:
    """The offense's side of the standard success-rate definition."""
    if _is_turnover(play):
        return False
    if play.get("scoring") and not _is_turnover(play):
        return True
    yards = int(play.get("yardsGained") or 0)
    down = int(play.get("down") or 0)
    distance = play.get("distance")
    if not distance or int(distance) <= 0:
        return yards > 0
    distance = int(distance)
    if down <= 1:
        return yards >= 0.5 * distance
    if down == 2:
        return yards >= 0.7 * distance
    return yards >= distance


def _family(play_type: str) -> str:
    text = (play_type or "").lower()
    if "rush" in text:
        return "rush"
    if "pass" in text or "sack" in text or "interception" in text:
        return "dropback"
    return "other"


def _type_rows(plays: list[dict]) -> list[dict]:
    """Per-play-type aggregate rows for one set of scrimmage plays."""
    groups: dict = {}
    for p in plays:
        groups.setdefault(p.get("playType") or "Unknown", []).append(p)
    rows = []
    for ptype, group in groups.items():
        yards = sum(int(p.get("yardsGained") or 0) for p in group)
        successes = sum(1 for p in group if _is_success(p))
        rows.append({
            "type": ptype,
            "plays": len(group),
            "yards": yards,
            "yards_per_play": round(yards / len(group), 2),
            "success_rate": round(successes / len(group) * 100, 1),
            "explosive_15plus": sum(1 for p in group
                                    if (p.get("yardsGained") or 0) >= 15),
            "stuffed_zero_or_less": sum(1 for p in group
                                        if (p.get("yardsGained") or 0) <= 0),
        })
    rows.sort(key=lambda r: -r["plays"])
    return rows


def _family_rows(plays: list[dict]) -> dict:
    out = {}
    for family in ("rush", "dropback"):
        group = [p for p in plays if _family(p.get("playType")) == family]
        if not group:
            continue
        yards = sum(int(p.get("yardsGained") or 0) for p in group)
        successes = sum(1 for p in group if _is_success(p))
        out[family] = {"plays": len(group), "yards": yards,
                       "yards_per_play": round(yards / len(group), 2),
                       "success_rate": round(successes / len(group) * 100, 1)}
    return out


def _play_type_breakdown(plays: list, home: str, away: str) -> dict:
    """Offense and defense play-type effectiveness for both teams."""
    scrimmage = [p for p in plays if _is_scrimmage(p)]

    def offense_of(team):
        return [p for p in scrimmage if p.get("offense") == team]

    out = {"definition": SUCCESS_DEFINITION}
    for team, opponent in ((home, away), (away, home)):
        out[team] = {
            # What this team ran, and how it went.
            "offense_by_type": _type_rows(offense_of(team)),
            "offense_rush_vs_dropback": _family_rows(offense_of(team)),
            # What this team's DEFENSE allowed, by the opponent's play type.
            "defense_allowed_by_type": _type_rows(offense_of(opponent)),
            "defense_rush_vs_dropback_allowed": _family_rows(offense_of(opponent)),
        }
    return out


def _player_execution(plays: list, play_stats: list, limit: int = 24) -> dict:
    """Play-type success rates by the players executing (and defending) them.

    /plays/stats ties athletes to individual plays, so joining on playId lets every
    play's success verdict be attributed to the players involved: the offense's
    players get the offensive verdict, defenders get the inverse.
    """
    by_id = {p.get("id"): p for p in plays if p.get("id") is not None and _is_scrimmage(p)}
    players: dict = {}
    joined = skipped = 0
    for row in play_stats or []:
        name = (row.get("athleteName") or "").strip()
        play = by_id.get(row.get("playId"))
        if not name or play is None:
            skipped += 1
            continue
        joined += 1
        team = row.get("team") or ""
        entry = players.setdefault((team, name), {
            "team": team, "player": name, "roles": set(),
            "offense": {}, "defense": {}, "_plays": set(),
        })
        if row.get("statType"):
            entry["roles"].add(row["statType"])
        # The same player can appear twice on one play (e.g. Completion + Touchdown);
        # count each play once per player.
        play_key = play.get("id")
        if play_key in entry["_plays"]:
            continue
        entry["_plays"].add(play_key)

        family = _family(play.get("playType"))
        side = "offense" if play.get("offense") == team else "defense"
        success = _is_success(play)
        if side == "defense":
            success = not success            # a stop is the defender's success
        bucket = entry[side].setdefault(family, {"plays": 0, "yards": 0, "successes": 0})
        bucket["plays"] += 1
        bucket["yards"] += int(play.get("yardsGained") or 0)
        bucket["successes"] += 1 if success else 0

    ranked = sorted(players.values(), key=lambda e: -len(e["_plays"]))[:limit]
    out_players = []
    for entry in ranked:
        for side in ("offense", "defense"):
            for family, bucket in entry[side].items():
                bucket["success_rate"] = round(
                    bucket["successes"] / bucket["plays"] * 100, 1)
                bucket["yards_per_play"] = round(bucket["yards"] / bucket["plays"], 2)
        out_players.append({
            "team": entry["team"], "player": entry["player"],
            "roles": sorted(entry["roles"]),
            "plays_involved": len(entry["_plays"]),
            "offense": entry["offense"], "defense": entry["defense"],
        })
    return {
        "note": ("Success attribution follows the play: offensive players carry the "
                 "play's verdict, defenders the inverse (a stopped play is the "
                 "defender's success). Ranked by plays involved."),
        "stat_rows_joined_to_plays": joined,
        "stat_rows_unjoinable": skipped,
        "players": out_players,
    }


def _player_lines(play_stats: list, limit: int = 40) -> list[dict]:
    """Aggregate the player-play associations into per-player stat lines."""
    totals: dict = {}
    for row in play_stats:
        name = row.get("athleteName")
        stat_type = row.get("statType")
        if not name or not stat_type:
            continue
        key = (row.get("team"), name, stat_type)
        try:
            totals[key] = totals.get(key, 0) + float(row.get("stat") or 0)
        except (TypeError, ValueError):
            continue
    lines: dict = {}
    for (team, name, stat_type), value in totals.items():
        entry = lines.setdefault((team, name), {"team": team, "player": name, "stats": {}})
        entry["stats"][stat_type] = round(value, 1)
    ranked = sorted(lines.values(),
                    key=lambda e: -sum(abs(v) for v in e["stats"].values()))
    return ranked[:limit]


def _build_prompt(ctx: dict, bundle: dict, chart_set: list, registry) -> str:
    sections = "\n".join(
        f"{i}. {title} — {guidance}" for i, (title, guidance) in enumerate(SECTIONS, 1)
    )
    charts = "\n".join(f'- "{c["title"]}" (rendered): {c["caption"]}' for c in chart_set)
    return f"""Write the complete Full Game Recap for this college football game.

Produce EXACTLY these sections, in this order, and nothing else. No preamble before the
first section, no sign-off after the last.

{sections}

FORMAT RULES:
- Each section heading is a level-2 markdown heading on its own line: "## Section Title".
- Open each section with one short line saying what it covers.
- Use markdown tables for statistical comparisons; bold lead-ins ("**{ctx['home_team']}.**")
  when a section covers the two teams in turn.
- Cite the data source with the pre-assigned marker [1] the first time each section
  leans on it; do not fabricate other citations.
- EVERY number must come from the DATA below. If a figure is not in the data, write
  around it — never estimate, never invent. No injuries, no quotes, no off-field
  context: this recap is built from the game record alone.

RENDERED CHARTS (already placed in the report — reference them, do not describe pixel
by pixel):
{charts}

PRE-ASSIGNED CITATION MARKERS:
[1] CollegeFootballData game record (game, advanced box score, drives, plays, player
    stat lines).

DATA:
{json.dumps(bundle, separators=(',', ':'), default=str)}
"""


def generate(
    *,
    game_id: int,
    settings: dict | None = None,
    watermark: str | None = None,
    report_dir: str | None = None,
    progress=None,
) -> dict:
    """Build one Full Game Recap end to end."""
    progress = progress or (lambda *a: None)
    settings = settings or accounts.effective_settings(None)
    started = time.time()
    current = {"stage": "start", "label": "Starting up"}

    def step(key):
        pct, label = STAGES[key]
        current["stage"], current["label"] = key, label
        progress(key, pct, label)

    generate.current_stage = current
    step("start")

    cfbd_api_key = db.resolve_cfbd_key()
    openrouter_api_key = db.resolve_openrouter_key()
    missing = [n for n, v in (("CFBD", cfbd_api_key), ("OpenRouter", openrouter_api_key)) if not v]
    if missing:
        raise PipelineError(f"Missing required API key(s): {', '.join(missing)}")

    # --- Stage 1: the whole game record --------------------------------------
    step("gather")
    recap = cfbd.fetch_game_recap(cfbd_api_key, game_id)

    auth = recap.get("auth_failures") or []
    if auth and len(auth) >= max(1, (recap.get("total_requests") or 1) // 2):
        first = auth[0]
        raise PipelineError("CollegeFootballData rejected the API key",
                            f"HTTP {first['status']} — {first['body']}", 502)

    game = recap.get("game") or {}
    if not game:
        raise PipelineError(f"Game {game_id} was not found on CollegeFootballData",
                            "Check the game id — /v1/games lists valid ones.", 404)
    if not game.get("completed"):
        raise PipelineError(
            f"Game {game_id} has not been completed yet",
            "A Full Game Recap needs a finished game. For a preview, use the "
            "matchup report.", 409)

    home, away = game.get("homeTeam") or "Home", game.get("awayTeam") or "Away"
    year = game.get("season")
    home_meta = cfbd.team_meta(recap.get("teams") or [], home)
    away_meta = cfbd.team_meta(recap.get("teams") or [], away)

    today = datetime.now()
    # "_vs_" in the filename, not a bare underscore: school names contain spaces
    # ("Ohio State"), and downstream subject parsing needs an unambiguous divider.
    # The game DATE rides along too — the same two teams can meet twice in a season,
    # and each meeting deserves its own recap.
    safe = "".join(ch for ch in f"{home}_vs_{away}" if ch.isalnum() or ch in " -_").strip()
    game_date_iso = str(game.get("startDate") or "")[:10]
    if game_date_iso:
        safe = f"{safe}_{game_date_iso}"
    out_dir = report_dir or config.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    filename = f"recap_{safe}_{db.format_friendly_date(today)}.pdf"
    filepath = os.path.join(out_dir, filename)
    tmp_path = filepath + ".building"

    plays = recap.get("plays") or []
    drives = recap.get("drives") or []
    playtypes = _play_type_breakdown(plays, home, away)
    bundle = {
        "game": {
            "id": game.get("id"),
            "season": year,
            "week": game.get("week"),
            "season_type": game.get("seasonType"),
            "date": game.get("startDate"),
            "venue": game.get("venue"),
            "neutral_site": game.get("neutralSite"),
            "conference_game": game.get("conferenceGame"),
            "attendance": game.get("attendance"),
            "home_team": home, "away_team": away,
            "home_points": game.get("homePoints"),
            "away_points": game.get("awayPoints"),
            "home_line_scores": game.get("homeLineScores"),
            "away_line_scores": game.get("awayLineScores"),
            "excitement_index": game.get("excitementIndex"),
            "home_postgame_win_probability": game.get("homePostgameWinProbability"),
            "home_pregame_elo": game.get("homePregameElo"),
            "home_postgame_elo": game.get("homePostgameElo"),
            "away_pregame_elo": game.get("awayPregameElo"),
            "away_postgame_elo": game.get("awayPostgameElo"),
        },
        "advanced_box_score": recap.get("box") or {},
        "drives": _compact_drives(drives),
        "notable_plays": _notable_plays(plays),
        "half_splits": _half_splits(plays, home, away),
        "play_type_breakdown": playtypes,
        "player_execution": _player_execution(plays, recap.get("play_stats") or []),
        "player_stat_lines": _player_lines(recap.get("play_stats") or []),
        "data_coverage": {
            "drives": len(drives), "plays": len(plays),
            "player_stat_rows": len(recap.get("play_stats") or []),
            "endpoints_with_errors": [e["label"] for e in recap.get("errors") or []],
        },
    }

    # --- Stage 2: visuals -----------------------------------------------------
    step("charts")
    try:
        chart_set = charts_mod.build_recap_charts(recap, home_meta, away_meta,
                                                  playtypes=playtypes)
    except charts_mod.ChartsUnavailable as e:
        raise PipelineError("Charting library missing on the server", str(e), 500)

    registry = research.SourceRegistry()
    registry.add("https://collegefootballdata.com", "College Football Data API",
                 "CollegeFootballData")

    ctx = {"home_team": home, "away_team": away, "year": year}

    # --- Stage 3: synthesis ----------------------------------------------------
    step("write")
    try:
        result = report_mod.generate(
            openrouter_api_key, ctx, bundle, chart_set, registry, settings,
            prompt=_build_prompt(ctx, bundle, chart_set, registry),
            system_prompt=SYSTEM_PROMPT,
        )
    except Exception as e:
        logging.exception("Recap generation failed")
        detail = str(e)
        body = getattr(e, "body", None)
        if body and str(body) not in detail:
            detail = f"{detail} | upstream: {body}"
        raise PipelineError("Report model request failed", detail[:500], 502)

    # --- Stage 4: render --------------------------------------------------------
    step("pdf")
    usage = result["usage"]
    final = (f"{home} {game.get('homePoints')}, {away} {game.get('awayPoints')}"
             if game.get("homePoints") is not None else f"{home} vs {away}")
    meta_lines = [
        f"Data: CollegeFootballData game {game.get('id')} — advanced box score, "
        f"{len(drives)} drives, {len(plays)} plays, "
        f"{len(recap.get('play_stats') or [])} player stat rows. No web research: "
        f"this recap is built from the game record alone.",
        f"Report: {result['model']} via OpenRouter — {usage.get('input_tokens') or 'N/A'} "
        f"input tokens / {usage.get('output_tokens') or 'N/A'} output tokens.",
        f"Final: {final}. Generation time: {int(time.time() - started)}s.",
    ]

    html = render.build_html(
        home_full=home,
        away_full=away,
        year=year or today.year,
        home_logo=home_meta.get("logo", ""),
        away_logo=away_meta.get("logo", ""),
        report_created=f"{db.format_friendly_date(today)} {today.strftime('%I:%M %p')}",
        report_markdown=result["text"],
        charts=chart_set,
        registry=registry,
        meta_lines=meta_lines,
        title=f"{home} vs {away} — Game Recap",
        banner="College Football Game Recap",
        include_sources=bool(settings.get("include_sources", 1)),
        include_generation_details=bool(settings.get("include_generation_details", 1)),
    )

    try:
        render.write_pdf(html, tmp_path,
                         footer_subject=f"{home} vs {away} — {year}",
                         footer_brand="Game Recap")
    except ImportError:
        raise PipelineError("PDF generation library not installed on server.", "", 500)
    except Exception as e:
        logging.error(f"PDF generation failed: {e}")
        raise PipelineError("PDF generation failed", str(e), 500)

    stamp = watermark or config.WATERMARK_PATH
    if os.path.exists(stamp):
        try:
            render.add_pdf_watermark(
                tmp_path, stamp,
                opacity=float(settings.get("watermark_opacity", config.WATERMARK_OPACITY)),
                scale=float(settings.get("watermark_scale", config.WATERMARK_SCALE)),
            )
        except Exception as e:
            logging.warning(f"Watermark failed; shipping unstamped: {e}")

    os.replace(tmp_path, filepath)
    elapsed = int(time.time() - started)
    step("done")
    logging.info(f"Game recap {filename} generated in {elapsed}s "
                 f"(game {game.get('id')}, {len(drives)} drives, {len(plays)} plays).")
    return {
        "filename": filename,
        "seconds": elapsed,
        "game_id": game.get("id"),
        "final": final,
        "drives": len(drives),
        "plays": len(plays),
    }
