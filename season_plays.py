"""Full Season Play-by-Play Analysis: every play a team ran and faced all year.

The recap answers "what happened in that game?"; this report answers "who IS this
team?" — the tendencies that only emerge across a whole season of play-by-play.
Like the recap it runs NO live web research: its entire diet is CollegeFootballData,
here every play from every completed game the team played in the chosen season, cut
by play type, rush direction, pass depth, down and distance, and game by game.
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
from playbook import team_breakdown, _agg, _family, _is_scrimmage

STAGES = {
    "start":  (5,   "Starting up"),
    "gather": (15,  "Pulling every play from every game this season"),
    "charts": (55,  "Rendering charts"),
    "write":  (65,  "Writing the analysis"),
    "pdf":    (92,  "Building the PDF"),
    "done":   (100, "Complete"),
}

SECTIONS = [
    ("Season Overview and Offensive Identity", (
        "The record, the season in one paragraph, and — from the play-by-play, not "
        "reputation — what this offense actually is: run-first or pass-first, its "
        "overall success rate and yards per play on both sides of the ball, and the "
        "one or two numbers that define the season. State the success-rate "
        "definition once, from the data."
    )),
    ("Play-Type Profile", (
        "What the offense ran all season and how well each call worked: every play "
        "type by volume, success rate, yards per play, explosives and stuffs, and "
        "the rush-versus-dropback split. Use a table for the type-level numbers. "
        "Contrast volume with efficiency — the types leaned on most are not always "
        "the ones that worked."
    )),
    ("The Ground Game: Where the Runs Went", (
        "From the rush-direction data: how the season's designed runs distributed "
        "across left/middle/right and end/tackle/guard, and how each direction "
        "fared — success rate, yards per carry, explosives, stuffs. Name the "
        "favourite gap and the most productive gap, and say whether they are the "
        "same. Scrambles are separated from designed runs; treat them as part of "
        "the dropback story. Directions marked 'unclassified' mean the play text "
        "named no gap — say so, never guess."
    )),
    ("The Passing Game: Depth, Pressure and Screens", (
        "From the passing detail: dropback volume and efficiency, short versus deep "
        "throws (volume and success of each), the screen game, sacks taken and "
        "interceptions thrown across the season. Characterise the passing identity "
        "— quick game, downfield, checkdown-heavy — strictly from these numbers."
    )),
    ("Down and Distance Tendencies", (
        "How play selection and effectiveness shifted by down across the season: "
        "rush share and success rate on 1st, 2nd, 3rd and 4th down. Call out "
        "predictability — a heavy run lean on early downs, an all-pass 3rd down — "
        "with the numbers that show it, and what that predictability cost or saved."
    )),
    ("Money Downs: Third and Fourth", (
        "Third-down conversion rates by distance (short 1-3, medium 4-6, long 7+), "
        "how the offense got to each bucket, and run/pass selection within each. "
        "Then fourth down: how often the team went for it, conversion rate, and "
        "what that says about the staff's aggressiveness. Red-zone efficiency "
        "closes the section: trips, touchdowns, success rate."
    )),
    ("The Other Side: What Opponents Did", (
        "The same deep cuts for the defense — everything opposing offenses ran "
        "against this team all season: play types allowed, where opponents ran and "
        "with what success, the depths they threw to, their down-by-down success "
        "and third-down conversions by distance. Name the defense's strengths and "
        "the soft spots opponents kept finding."
    )),
    ("Week-to-Week Evolution", (
        "From the game log: how the offensive and defensive profiles moved across "
        "the season — success rate, yards per play and rush share game by game. "
        "Identify real trends (an offense that opened up, a defense that faded) "
        "versus one-game blips, and tie shifts to the opposition faced. Compare the "
        "profile in wins against the profile in losses."
    )),
    ("The Scouting Report", (
        "Close as an opposing coordinator would: the three to five tendencies an "
        "opponent should attack — each one cited numerically from the data above — "
        "and the two or three genuine strengths an opponent must scheme around. "
        "Strictly what this season's play-by-play supports; no roster speculation, "
        "no next-season projection."
    )),
]

SYSTEM_PROMPT = (
    "You are a college football analyst writing a season-long play-by-play study "
    "from verified data. You write with authority and specificity: every claim is "
    "anchored to the supplied numbers, and you never invent events, players, "
    "injuries or context that is not in the data. Plain, confident, readable "
    "prose — no hedging filler."
)


def _game_log(games: list, plays: list, team: str) -> list[dict]:
    """Per-game compact profile rows, the evidence for the evolution section."""
    by_game: dict = {}
    for p in plays:
        if _is_scrimmage(p):
            by_game.setdefault(p.get("gameId"), []).append(p)

    def side_line(rows):
        if not rows:
            return {"plays": 0}
        agg = _agg(rows)
        rushes = sum(1 for p in rows if _family(p.get("playType")) == "rush")
        return {
            "plays": agg["plays"],
            "yards_per_play": agg["yards_per_play"],
            "success_rate": agg["success_rate"],
            "rush_share_pct": round(rushes / len(rows) * 100, 1),
        }

    out = []
    for g in sorted(games, key=lambda g: (str(g.get("startDate") or ""),
                                          g.get("week") or 0)):
        home = cfbd.pick(g, "homeTeam", "home_team", default="")
        away = cfbd.pick(g, "awayTeam", "away_team", default="")
        hp = cfbd.pick(g, "homePoints", "home_points")
        ap = cfbd.pick(g, "awayPoints", "away_points")
        is_home = home == team
        pf, pa = (hp, ap) if is_home else (ap, hp)
        rows = by_game.get(g.get("id")) or []
        out.append({
            "week": g.get("week"),
            "season_type": cfbd.pick(g, "seasonType", "season_type", default="regular"),
            "date": str(cfbd.pick(g, "startDate", "start_date", default=""))[:10],
            "opponent": away if is_home else home,
            "site": "home" if is_home else "away",
            "result": ("W" if pf > pa else "L" if pf < pa else "T")
                      if pf is not None and pa is not None else None,
            "score": f"{pf}-{pa}" if pf is not None else None,
            "offense": side_line([p for p in rows if p.get("offense") == team]),
            "defense_allowed": side_line([p for p in rows if p.get("offense") != team]),
        })
    return out


def _wins_losses_split(game_log: list[dict], plays: list, games: list, team: str) -> dict:
    """The offensive profile in wins vs in losses — same aggregate, split by result."""
    result_by_game: dict = {}
    for g in games:
        home = cfbd.pick(g, "homeTeam", "home_team", default="")
        hp = cfbd.pick(g, "homePoints", "home_points")
        ap = cfbd.pick(g, "awayPoints", "away_points")
        if hp is None or ap is None:
            continue
        won_as_home = hp > ap
        result_by_game[g.get("id")] = won_as_home if home == team else not won_as_home

    out = {}
    for label, wanted in (("in_wins", True), ("in_losses", False)):
        rows = [p for p in plays if _is_scrimmage(p) and p.get("offense") == team
                and result_by_game.get(p.get("gameId")) is wanted]
        if not rows:
            continue
        rushes = sum(1 for p in rows if _family(p.get("playType")) == "rush")
        out[label] = {**_agg(rows),
                      "rush_share_pct": round(rushes / len(rows) * 100, 1)}
    return out


def _build_prompt(ctx: dict, bundle: dict, chart_set: list) -> str:
    sections = "\n".join(
        f"{i}. {title} — {guidance}" for i, (title, guidance) in enumerate(SECTIONS, 1)
    )
    charts = "\n".join(f'- "{c["title"]}" (rendered): {c["caption"]}' for c in chart_set)
    return f"""Write the complete Full Season Play-by-Play Analysis for {ctx['team_full']}'s {ctx['year']} season.

Produce EXACTLY these sections, in this order, and nothing else. No preamble before the
first section, no sign-off after the last.

{sections}

FORMAT RULES:
- Each section heading is a level-2 markdown heading on its own line: "## Section Title".
- Open each section with one short line saying what it covers.
- Use markdown tables for statistical comparisons; bold lead-ins for sub-topics.
- Cite the data source with the pre-assigned marker [1] the first time each section
  leans on it; do not fabricate other citations.
- EVERY number must come from the DATA below. If a figure is not in the data, write
  around it — never estimate, never invent. No injuries, no quotes, no off-field
  context: this analysis is built from the season's play-by-play alone.

RENDERED CHARTS (already placed in the report — reference them, do not describe pixel
by pixel):
{charts}

PRE-ASSIGNED CITATION MARKERS:
[1] CollegeFootballData play-by-play (every play from every completed {ctx['team_short']}
    game in {ctx['year']}).

DATA:
{json.dumps(bundle, separators=(',', ':'), default=str)}
"""


def generate(
    *,
    team_full: str,
    team_short: str,
    year: int | None = None,
    settings: dict | None = None,
    watermark: str | None = None,
    report_dir: str | None = None,
    progress=None,
) -> dict:
    """Build one Full Season Play-by-Play Analysis end to end."""
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

    today = datetime.now()
    if not year:
        year = cfbd.season_year(today)

    # --- Stage 1: every play of the season -----------------------------------
    step("gather")
    season = cfbd.fetch_season_plays(cfbd_api_key, year, team_short)

    auth = season.get("auth_failures") or []
    if auth and len(auth) >= max(1, (season.get("total_requests") or 1) // 2):
        first = auth[0]
        raise PipelineError("CollegeFootballData rejected the API key",
                            f"HTTP {first['status']} — {first['body']}", 502)

    games = season.get("games") or []
    plays = season.get("plays") or []
    if not games:
        raise PipelineError(
            f"No completed games found for {team_short} in {year}",
            "The season play-by-play analysis needs at least one finished game. "
            "Check the team name and season.", 404)
    if not plays:
        raise PipelineError(
            f"CollegeFootballData returned no plays for {team_short} in {year}",
            "The games exist but the play-by-play feed is empty for them.", 502)

    meta = cfbd.resolve_team_meta(cfbd_api_key, season.get("teams") or [], team_short, year)

    safe = "".join(ch for ch in team_short if ch.isalnum() or ch in " -_").strip() or "team"
    out_dir = report_dir or config.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    filename = f"plays_{safe}_{year}_{db.format_friendly_date(today)}.pdf"
    filepath = os.path.join(out_dir, filename)
    tmp_path = filepath + ".building"

    breakdown = team_breakdown(plays, team_short)
    game_log = _game_log(games, plays, team_short)
    wins = sum(1 for g in game_log if g.get("result") == "W")
    losses = sum(1 for g in game_log if g.get("result") == "L")

    bundle = {
        "team": {
            "name": team_full,
            "school": team_short,
            "conference": meta.get("conference"),
            "classification": meta.get("classification"),
            "season": year,
            "record": f"{wins}-{losses}",
        },
        "season_breakdown": breakdown,
        "game_log": game_log,
        "offense_in_wins_vs_losses": _wins_losses_split(game_log, plays, games, team_short),
        "data_coverage": {
            "completed_games": len(games),
            "plays": len(plays),
            "endpoints_with_errors": [e["label"] for e in season.get("errors") or []],
        },
    }

    # --- Stage 2: visuals -----------------------------------------------------
    step("charts")
    try:
        chart_set = charts_mod.build_season_play_charts(
            breakdown, game_log, meta, team_short)
    except charts_mod.ChartsUnavailable as e:
        raise PipelineError("Charting library missing on the server", str(e), 500)

    registry = research.SourceRegistry()
    registry.add("https://collegefootballdata.com", "College Football Data API",
                 "CollegeFootballData")

    ctx = {"team_full": team_full, "team_short": team_short, "year": year}

    # --- Stage 3: synthesis ----------------------------------------------------
    step("write")
    try:
        result = report_mod.generate(
            openrouter_api_key, ctx, bundle, chart_set, registry, settings,
            prompt=_build_prompt(ctx, bundle, chart_set),
            system_prompt=SYSTEM_PROMPT,
        )
    except Exception as e:
        logging.exception("Season play analysis generation failed")
        detail = str(e)
        body = getattr(e, "body", None)
        if body and str(body) not in detail:
            detail = f"{detail} | upstream: {body}"
        raise PipelineError("Report model request failed", detail[:500], 502)

    # --- Stage 4: render --------------------------------------------------------
    step("pdf")
    usage = result["usage"]
    meta_lines = [
        f"Data: CollegeFootballData play-by-play — {len(plays)} plays across "
        f"{len(games)} completed games, {year} season. No web research: this "
        f"analysis is built from the play-by-play record alone.",
        f"Report: {result['model']} via OpenRouter — {usage.get('input_tokens') or 'N/A'} "
        f"input tokens / {usage.get('output_tokens') or 'N/A'} output tokens.",
        f"Record: {wins}-{losses}. Generation time: {int(time.time() - started)}s.",
    ]

    html = render.build_html(
        home_full=team_full,
        away_full="",
        year=year,
        home_logo=meta.get("logo", ""),
        away_logo="",
        report_created=f"{db.format_friendly_date(today)} {today.strftime('%I:%M %p')}",
        report_markdown=result["text"],
        charts=chart_set,
        registry=registry,
        meta_lines=meta_lines,
        title=f"{team_full} {year} — Season Play-by-Play Analysis",
        banner="Full Season Play-by-Play Analysis",
        include_sources=bool(settings.get("include_sources", 1)),
        include_generation_details=bool(settings.get("include_generation_details", 1)),
    )

    try:
        render.write_pdf(html, tmp_path,
                         footer_subject=f"{team_full} — {year}",
                         footer_brand="Season Play-by-Play")
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
    logging.info(f"Season play analysis {filename} generated in {elapsed}s "
                 f"({team_short} {year}: {len(games)} games, {len(plays)} plays).")
    return {
        "filename": filename,
        "seconds": elapsed,
        "team": team_short,
        "year": year,
        "games": len(games),
        "plays": len(plays),
    }
