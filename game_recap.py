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
    ("Pre-Game Conditions and Expectations", (
        "What everyone expected walking in, from the pregame_expectations data: the "
        "betting lines (spread and total, by book), the market's implied win "
        "probability, the pregame Elo gap, the weather (temperature, wind, "
        "precipitation, indoors) and what those conditions promised for the style of "
        "game, and the venue block — surface, dome or open air, capacity, elevation "
        "— plus the broadcast. Close with one line framing the "
        "expectation the rest of this recap grades reality against. Any feed marked "
        "unavailable gets one clause, never an invented number."
    )),
    ("Final Score and Game Story", (
        "The result, stated immediately: final score, where and when, and the one-"
        "paragraph story of the game — was it controlled throughout, a comeback, a "
        "collapse, decided late? Use the excitement index and win-probability swing "
        "to characterise it honestly, and say plainly whether the favorite covered "
        "where the pregame line is present."
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
        "bought in points. Where the win-probability data is available, anchor the "
        "turning points to its biggest swings — the plays that actually moved the "
        "game — and quote the swing size."
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
    ("Coaching and Play-Calling Grades", (
        "Each staff's play-calling, graded. The coach_playcalling data carries a "
        "COMPUTED grade for each team on the school scale (F- to A+), built from "
        "weighted components: early-down success, schedule management, third-down "
        "conversion, explosive-play creation, negative-play avoidance, red-zone "
        "finishing, and a fourth-down decision adjustment. State each team's letter "
        "grade in bold in the first line of its write-up, then walk the two or three "
        "components that earned it — citing the component values — and compare the "
        "two staffs. The grade is deterministic: present it as given, explain it, "
        "never re-derive or soften it."
    )),
    ("What Could Have Been Done Differently", (
        "Grounded counterfactuals only — each one anchored to something in the data: "
        "points left on the field in scoring opportunities, field-position surrendered, "
        "personnel usage the impact numbers argue against. No speculation about "
        "injuries, play-calling intent, or anything the data cannot see."
    )),
    ("Player and Unit Grades", (
        "Individual and unit impact, both ends of the scale. From player_impact and "
        "the player stat lines: the players who moved the game — AND the "
        "most_negative list, the players whose touches cost their team expected "
        "points, named with the same specificity as the heroes. From "
        "player_execution: whose play types kept succeeding or failing. Then the "
        "unit_grades ranking: all eight units (each team's rushing/passing offense "
        "and run/pass defense) best to worst on per-play value — present the "
        "ranking as a table and call out the best and worst unit on the field."
    )),
    ("Play-Type Breakdown", (
        "What each team ran and how well it worked, from the play-group data: "
        "Rushes (including rushing TDs, scrambles noted), Passes (one group — "
        "completions, incompletions, passing TDs and interceptions together, with "
        "the completion detail inside the row), and Sacks — success rate, yards per "
        "play, avg PPA, explosives and stuffs for each group, the OFFENSIVE view "
        "for each team, then the DEFENSIVE view (what each defense allowed). "
        "The success definition is supplied in the data; state it once. Then the "
        "players executing: from the player-execution data, which ball-carriers, "
        "passers and targets drove each group's success rate up or down, and which "
        "defenders kept showing up in the plays that failed. Use tables for the "
        "group-level numbers; name names in the prose. Where avg_ppa is present, use "
        "it as the value measure — a play group can move the chains while losing "
        "expected points, and PPA is what exposes that."
    )),
    ("Down, Distance and Direction", (
        "The deeper cut, from the situational breakdown: WHERE each ground game went "
        "(left/middle/right, end/tackle/guard) and how each direction fared — but "
        "check rush_direction_coverage first: below 25% classified, say once that "
        "the play text names no gaps and analyse designed_rush_outcomes instead of "
        "direction tendencies; scrambles "
        "and screens separated from designed plays; how play selection and success "
        "shifted by down; third-down conversion rates by distance (short 1-3, medium "
        "4-6, long 7+); fourth-down attempts and results; red-zone efficiency; and "
        "the negative_plays ledger — every snap that went backwards, not just sacks: "
        "rushes for loss, sacks, other losses, yards surrendered and turnovers. Say "
        "who won the negative-play battle and what it cost. Cover "
        "BOTH teams, offense and what each defense allowed. Cite tendencies "
        "numerically — success running left vs right, run rate on 2nd-and-long — and "
        "flag anything a future opponent should attack. Directions marked "
        "'unclassified' mean the play text named no gap; say so rather than guessing."
    )),
    ("Advanced Box Score Analysis", (
        "The full statistical autopsy: efficiency, explosiveness, field position, "
        "havoc, scoring opportunities, rushing and passing splits. Present the "
        "comparisons as a table where that is clearer than prose. When the live "
        "advanced layer is available, add its second dimension: line yards vs "
        "second-level vs open-field rushing (who won the trenches vs who broke free), "
        "EPA per rush and per dropback, standard-down vs passing-down success, "
        "success with garbage time removed, and the 'deserve to win' verdict against "
        "the actual result. If it is unavailable, simply omit all of that."
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


def _wp_value(row: dict):
    """The home win probability of one row, whatever the API spelled it or typed it.

    The published spec says homeWinProbability (number), but a report that silently
    loses its whole win-probability layer to a field-name or string-typing quirk is
    worse than a tolerant parser — so accept the plausible variants and coerce."""
    for key in ("homeWinProbability", "homeWinProb", "home_win_probability"):
        value = row.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _win_probability(rows: list, home: str, away: str) -> dict:
    """The stored in-game win-probability curve, and the plays that moved it most."""
    usable = [(_wp_value(r), r) for r in rows or []]
    usable = [(wp, r) for wp, r in usable if wp is not None]
    if not usable:
        return {"available": False,
                "note": (f"No win-probability series is stored for this game "
                         f"({len(rows or [])} rows returned)."),
                }

    def order(pair):
        _wp, r = pair
        try:
            return (0, int(r.get("playNumber")))
        except (TypeError, ValueError):
            pass
        try:
            return (1, int(r.get("playId")))
        except (TypeError, ValueError):
            return (2, 0)
    usable.sort(key=order)

    swings = []
    prev = usable[0][0]
    for wp, r in usable[1:]:
        swings.append((wp - prev, wp, r))
        prev = wp
    swings.sort(key=lambda t: -abs(t[0]))

    step = max(1, len(usable) // 40)     # ~40 samples describe the curve fully
    return {
        "available": True,
        "note": (f"home_wp is {home}'s win probability, play by play. The biggest "
                 f"swings are the game's true turning points — cite them."),
        "pregame_spread": usable[0][1].get("spread"),
        "curve": [{"play": int(r.get("playNumber") or 0), "home_wp": round(wp, 3)}
                  for wp, r in usable[::step]],
        "final_home_wp": round(usable[-1][0], 3),
        "biggest_swings": [{
            "swing_toward": home if delta > 0 else away,
            "delta_home_wp": round(delta, 3),
            "home_wp_after": round(wp, 3),
            "score_after": f"{r.get('homeScore')}-{r.get('awayScore')}",
            "text": (r.get("playText") or "")[:200],
        } for delta, wp, r in swings[:8]],
    }


def _pregame(recap: dict, game: dict, home: str, away: str,
             venue: dict | None = None) -> dict:
    """What everyone expected walking in: lines, market win%, weather, broadcast."""
    game_id = game.get("id")

    def row_for(rows):
        return next((r for r in rows or []
                     if r.get("id") == game_id or r.get("gameId") == game_id), None)

    weather = row_for(recap.get("weather"))
    if weather:
        weather = {k: weather.get(k) for k in (
            "gameIndoors", "venue", "temperature", "dewPoint", "humidity",
            "precipitation", "snowfall", "windDirection", "windSpeed",
            "weatherCondition") if weather.get(k) is not None}
    outlets = sorted({r.get("outlet") for r in recap.get("media") or []
                      if r.get("id") == game_id and r.get("outlet")})

    lines_row = row_for(recap.get("lines")) or {}
    books = [{"provider": l.get("provider"), "spread": l.get("spread"),
              "over_under": l.get("overUnder"),
              "formatted_spread": l.get("formattedSpread")}
             for l in lines_row.get("lines") or []][:5]

    wp = row_for(recap.get("wp_pregame")) or {}
    return {
        "note": ("The pregame picture: what the books, the market model and the "
                 "conditions said before kickoff. The recap grades reality against "
                 "this."),
        "weather": weather or {"available": False,
                               "note": "No conditions stored for this game."},
        "venue": venue or {"available": False,
                           "note": "Venue details unavailable."},
        "broadcast": outlets or None,
        "betting_lines": books or None,
        "market_pregame_home_win_probability": wp.get("homeWinProbability"),
        "market_spread": wp.get("spread"),
        "pregame_elo": {home: game.get("homePregameElo"),
                        away: game.get("awayPregameElo")},
    }


def _player_impact(box: dict, limit: int = 6) -> dict:
    """Both ends of the individual-impact scale, from the box-score player PPA."""
    rows = ((box or {}).get("players") or {}).get("ppa") or []
    scored = []
    for r in rows:
        total = ((r.get("cumulative") or {}).get("total")
                 if isinstance(r.get("cumulative"), dict) else None)
        if total is None and isinstance(r.get("average"), dict):
            total = (r.get("average") or {}).get("total")
        if total is None or not r.get("player"):
            continue
        scored.append({"player": r.get("player"), "position": r.get("position"),
                       "team": r.get("team"), "total_ppa": round(float(total), 2)})
    scored.sort(key=lambda e: -e["total_ppa"])
    return {
        "note": ("Cumulative PPA across every play a player touched. The negative "
                 "list is the players whose touches cost their team expected points "
                 "— name them with the same specificity as the heroes."),
        "most_positive": [e for e in scored if e["total_ppa"] > 0][:limit],
        "most_negative": sorted([e for e in scored if e["total_ppa"] < 0],
                                key=lambda e: e["total_ppa"])[:limit],
    }


def _live_layer(live: dict | None) -> dict:
    """The live-pipeline enrichment: pre-classified plays and line-level team stats.

    /live/plays is served from CFBD's live ingestion store, so coverage is strongest
    for recent games and routinely absent for older seasons. Everything here is
    additive — the recap stands entirely on the classic play-by-play without it.
    """
    if not isinstance(live, dict) or not live.get("teams"):
        return {"available": False,
                "note": ("The live play-by-play layer is not stored for this game "
                         "(typical for older seasons). The analysis rests on the "
                         "classic play-by-play; do not mention this gap in prose.")}

    teams = [{k: v for k, v in t.items() if k != "drives"}
             for t in live.get("teams") or []]
    plays = [p for drive in live.get("drives") or []
             for p in drive.get("plays") or []]

    def agg(rows):
        if not rows:
            return None
        epa = []
        for p in rows:
            try:
                if p.get("epa") is not None:
                    epa.append(float(p["epa"]))
            except (TypeError, ValueError):
                continue
        return {"plays": len(rows),
                "success_rate": round(
                    sum(1 for p in rows if p.get("success")) / len(rows) * 100, 1),
                "avg_epa": round(sum(epa) / len(epa), 3) if epa else None}

    classified: dict = {}
    for t in live.get("teams") or []:
        name = t.get("team")
        rows = [p for p in plays if p.get("team") == name]
        entry = {}
        for down_type in ("standard", "passing"):
            bucket = agg([p for p in rows
                          if (p.get("downType") or "").lower() == down_type])
            if bucket:
                entry[f"{down_type}_downs"] = bucket
        for rush_pass in ("rush", "pass"):
            bucket = agg([p for p in rows
                          if (p.get("rushPass") or "").lower() == rush_pass])
            if bucket:
                entry[rush_pass] = bucket
        real = agg([p for p in rows if not p.get("garbageTime")])
        if real:
            entry["excluding_garbage_time"] = real
        if entry:
            classified[name] = entry

    return {
        "available": True,
        "note": ("Pre-classified by CFBD's live pipeline: per-play EPA, success, "
                 "standard vs passing downs, garbage time flagged. Team blocks carry "
                 "line yards / second-level / open-field rushing splits, EPA splits "
                 "and 'deserve to win'."),
        "teams": teams,
        "play_classification": classified,
    }


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
# Play-type analytics — shared with the season play-by-play report
# ---------------------------------------------------------------------------
from playbook import (            # noqa: E402  (re-exported for callers and tests)
    SUCCESS_DEFINITION,
    _family,
    _is_scrimmage,
    _is_success,
    _is_turnover,
    play_type_breakdown as _play_type_breakdown,
    playcalling_report,
    situational_breakdown as _situational_breakdown,
    unit_report,
)


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
  Write every section EXACTLY ONCE — never repeat a section.
- Open each section with one short line saying what it covers.
- Use markdown tables for statistical comparisons; bold lead-ins ("**{ctx['home_team']}.**")
  when a section covers the two teams in turn. Every table starts on its own line with a
  blank line before it, one row per line — a table glued to prose does not render.
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
    home_meta = cfbd.resolve_team_meta(cfbd_api_key, recap.get("teams") or [], home, year)
    away_meta = cfbd.resolve_team_meta(cfbd_api_key, recap.get("teams") or [], away, year)

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
    venue = cfbd.venue_details(cfbd_api_key, venue_id=game.get("venueId"),
                               name=game.get("venue"))
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
        "pregame_expectations": _pregame(recap, game, home, away, venue=venue),
        "drives": _compact_drives(drives),
        "notable_plays": _notable_plays(plays),
        "half_splits": _half_splits(plays, home, away),
        "win_probability": _win_probability(recap.get("wp") or [], home, away),
        "play_type_breakdown": playtypes,
        "situational_breakdown": _situational_breakdown(plays, home, away),
        "coach_playcalling": {home: playcalling_report(plays, home),
                              away: playcalling_report(plays, away)},
        "unit_grades": unit_report(plays, home, away),
        "live_advanced": _live_layer(recap.get("live")),
        "player_impact": _player_impact(recap.get("box") or {}),
        "player_execution": _player_execution(plays, recap.get("play_stats") or []),
        "player_stat_lines": _player_lines(recap.get("play_stats") or []),
        "data_coverage": {
            "drives": len(drives), "plays": len(plays),
            "player_stat_rows": len(recap.get("play_stats") or []),
            "win_probability_points": len(recap.get("wp") or []),
            "live_layer_available": bool((recap.get("live") or {}).get("teams")),
            "endpoints_with_errors": [e["label"] for e in recap.get("errors") or []],
            "optional_endpoints_with_errors": [
                f"{e['label']} (HTTP {e['status']})"
                for e in recap.get("optional_errors") or []],
        },
    }

    # --- Stage 2: visuals -----------------------------------------------------
    step("charts")
    try:
        chart_set = charts_mod.build_recap_charts(
            recap, home_meta, away_meta, playtypes=playtypes,
            conditions={"weather": bundle["pregame_expectations"]["weather"],
                        "venue": venue})
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
    optional_failures = ", ".join(
        f"{e['label']} (HTTP {e['status']})"
        for e in recap.get("optional_errors") or []) or "none"
    meta_lines = [
        f"Data: CollegeFootballData game {game.get('id')} — advanced box score, "
        f"{len(drives)} drives, {len(plays)} plays, "
        f"{len(recap.get('play_stats') or [])} player stat rows. No web research: "
        f"this recap is built from the game record alone.",
        f"Enrichment: {len(recap.get('wp') or [])} win-probability points; live "
        f"layer {'present' if (recap.get('live') or {}).get('teams') else 'absent'}; "
        f"optional endpoint failures: {optional_failures}.",
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
