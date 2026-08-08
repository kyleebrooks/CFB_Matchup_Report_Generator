"""Single-team report: the first report type on the multi-tenant /v1 API.

Same shape as the matchup report — dedicated live-web research call per news-driven
section, CFBD for every number, one synthesis call to write the prose — but scoped to
one program and organised around its season rather than a single game.
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import accounts
import cfbd
import charts as charts_mod
import config
import db
import injuries
import render
import report as report_mod
import research
from pipeline import PipelineError

STAGES = {
    "start":    (5,   "Starting up"),
    "gather":   (12,  "Pulling CFBD statistics and running live web searches"),
    "assemble": (50,  "Compiling sources"),
    "charts":   (60,  "Rendering charts"),
    "write":    (70,  "Writing the report"),
    "pdf":      (92,  "Building the PDF"),
    "done":     (100, "Complete"),
}

# Section order is fixed. Each entry: (heading, research job key or None, guidance).
SECTIONS = [
    ("Overall Outlook", None,
     "Where this program stands right now: record, trajectory, what the ratings and "
     "efficiency numbers say, and the two or three things that will decide how the rest "
     "of the season goes. Synthesise from everything below — this section has no research "
     "feed of its own."),
    ("The Season: Played and Projected", "schedule",
     "One consolidated walk through the whole schedule — no separate season-so-far and "
     "road-ahead sections, this covers both. FIRST the games already played: a markdown "
     "table of results (Week, Opponent, H/A, Result, Score) followed by a short account "
     "of how each game actually went, drawing on the schedule research. THEN the road "
     "ahead: a table of every remaining game with its projected margin and win "
     "probability from the remaining-schedule outlook data, the projected final record, "
     "the likeliest stumble, and the stretch that decides the season. Where the graded "
     "prediction record for this team's games is present, summarise honestly how "
     "projections on this team have fared. Every result, margin and probability comes "
     "from the data [1] — never invent one — and remind the reader the projections are "
     "ratings-based estimates, not promises."),
    ("Practice Notes", "practice",
     "Practice and scrimmage reporting: who is standing out, who is limited, install and "
     "game-plan notes, position battles."),
    ("Roster News", "roster",
     "NON-INJURY roster movement: depth-chart changes, starter announcements, transfers in "
     "and out, suspensions, eligibility rulings, position switches. The transfer_portal "
     "block is the verified wire for this team's portal moves — present it as its own "
     "labeled group (with star ratings) before the researched news."),
    ("Injury Report", "injuries",
     "Medical items only, with status and expected return where reported. This section has "
     "TWO inputs — a live web search run just now, and the stored injury feed built up for "
     "this team over recent days — and they must be presented as "
     "clearly labeled separate groups."),
    ("What the Media Is Saying", "media",
     "National and local coverage, analyst takes, rankings movement, and betting-market "
     "sentiment about this team."),
    ("What the Coaches Are Saying", "coaches",
     "Direct quotes and paraphrases from the head coach and coordinators — press "
     "conferences, radio shows, media availabilities. Attribute every quote to who said it "
     "and when."),
]


def _jobs(ctx: dict) -> list[dict]:
    """Build the research jobs. Each carries its own fully-formed prompt."""
    team = ctx["team_full"]
    now = ctx["now_utc"]
    stamp = now.strftime("%A, %B %d, %Y at %H:%M UTC")

    def prompt(topic: str, focus: str, exclude: str, window: int) -> str:
        return f"""CURRENT DATE AND TIME: {stamp}
SEASON: {ctx['year']} college football season.
TEAM: {team}.

TASK: Research the "{team}" college football team and report ONLY on: {focus}.

SEARCH REQUIREMENTS — do this before you answer:
1. Run MULTIPLE web searches. Start broad, then follow up on every specific player name,
   storyline or quote you turn up. Do not stop after one search.
2. Hunt for BREAKING, UP-TO-THE-MINUTE news. Explicitly search for today's and
   yesterday's reporting and weight the newest items highest. Include beat writers, team
   sites and local outlets, not just national wires.
3. OPEN AND READ the most relevant results. Base every finding on the article's actual
   content, never on a search-result snippet or your own prior knowledge.
4. Cross-check anything surprising against a second source before reporting it.

HARD CONSTRAINTS:
- RECENCY: only items published within the last {window} days, as of the date above.
  Discard anything older, and anything you cannot date.
- CURRENT PLAYERS AND STAFF ONLY: no former players, alumni, or recruits who have not
  enrolled.
- SCOPE: exclude {exclude}.
- NO FABRICATION: every finding needs a real URL you actually retrieved. If you cannot
  source it, leave it out. An empty result is correct and useful; an invented one is not.
- If nothing qualifies, return {{"findings": [], "no_data": true}} and say why in "notes".

OUTPUT — a single JSON object, no prose outside it, no markdown fences:
{{
  "as_of_utc": "{now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
  "no_data": false,
  "notes": "brief coverage note: what you searched, and any gaps",
  "findings": [
    {{
      "headline": "short factual headline",
      "detail": "2-4 sentences of specifics drawn from the article itself",
      "player": "player or coach name, or empty string",
      "position": "position or role, or empty string",
      "team": "{team}",
      "status": "e.g. Out, Questionable, Starter named, Suspended, Transferred — or empty",
      "impact": "one sentence on what this means for the team",
      "published": "YYYY-MM-DD publication date of the article",
      "confidence": "high | medium | low",
      "source_name": "outlet name",
      "source_url": "full https URL you actually read"
    }}
  ]
}}

Order findings most-recent and most-impactful first. Cap at 12; quality over quantity.
Topic label for your own reference: {topic}."""

    return [
        {"key": "schedule", "prompt": prompt(
            "schedule",
            f"how {team}'s games this season have actually played out — game recaps, what "
            f"decided each result, and previews or early looks at their UPCOMING opponents",
            "opponent-only coverage that never mentions this team",
            30)},
        {"key": "practice", "prompt": prompt(
            "practice",
            f"practice reports, scrimmage results, camp observations, players held out or "
            f"limited, install and game-plan notes for {team}",
            "game recaps and pure injury reporting with no practice-participation angle",
            config.PRACTICE_WINDOW_DAYS)},
        {"key": "roster", "prompt": prompt(
            "roster",
            f"NON-INJURY roster news for {team}: depth-chart changes, starter announcements, "
            f"quarterback decisions, suspensions, transfer portal moves, eligibility rulings, "
            f"position switches, staff changes affecting who plays",
            "anything injury or medical related (that belongs in the injury section)",
            config.ROSTER_WINDOW_DAYS)},
        {"key": "injuries", "prompt": prompt(
            "injuries",
            f"injuries, injury designations, availability rulings, surgeries, return "
            f"timelines and week-to-week statuses for current {team} players",
            "suspensions, transfers and depth-chart moves with no medical component",
            config.INJURY_WINDOW_DAYS)},
        {"key": "media", "prompt": prompt(
            "media",
            f"national and local media coverage, analyst takes, poll and ranking movement, "
            f"and betting-market sentiment about {team}",
            "coverage of other programs that only mentions this team in passing",
            config.MEDIA_WINDOW_DAYS)},
        {"key": "coaches", "prompt": prompt(
            "coaches",
            f"direct quotes from {team}'s head coach and coordinators — press conferences, "
            f"weekly pressers, radio shows, media availabilities. Capture what was actually "
            f"said, who said it, and when",
            "quotes from opposing coaches or from analysts who are not on this staff",
            config.MEDIA_WINDOW_DAYS)},
    ]


def _assemble(raw: dict, rotowire: list, registry, ctx: dict, settings: dict) -> dict:
    """Merge research output and the stored injury feed into per-section buckets."""
    model = settings["research_model"]

    def bucket(key: str) -> dict:
        data = raw.get(key) or {}
        items = []
        for f in data.get("findings", []):
            idx = registry.add(f.get("source_url", ""), f.get("headline", ""), f.get("source_name", ""))
            item = dict(f)
            item["citation"] = f"[{idx}]" if idx else ""
            items.append(item)
        pages = []
        for c in data.get("citations", []):
            idx = registry.add(c.get("url", ""), c.get("title", ""), "")
            if idx:
                pages.append({"citation": f"[{idx}]", "title": c.get("title", ""), "url": c.get("url", "")})
        return {
            "source": f"Live web research ({model})",
            "retrieved_at_utc": data.get("as_of_utc") or ctx["now_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "no_data": data.get("no_data", True),
            "notes": data.get("notes", ""),
            "findings": items,
            "pages_retrieved": pages,
        }

    for item in rotowire:
        idx = registry.add(item.get("source_url", ""),
                           item.get("headline") or "College football injury feed",
                           item.get("source_name") or "Injury feed")
        item["citation"] = f"[{idx}]" if idx else "[2]"

    return {
        "schedule_research": {"section_title": "The Season: Played and Projected",
                              "inputs": {"live_web_research": bucket("schedule")}},
        "practice_notes": {"section_title": "Practice Notes",
                           "inputs": {"live_web_research": bucket("practice")}},
        "roster_news": {"section_title": "Roster News",
                        "inputs": {"live_web_research": bucket("roster")}},
        "injury_report": {"section_title": "Injury Report",
                          "inputs": {
                              "live_web_research": bucket("injuries"),
                              "rotowire_feed": {
                                  "source": ("Stored injury feed — items collected for this "
                                             "team and kept in the local database"),
                                  "window_days": config.INJURY_WINDOW_DAYS,
                                  "no_data": not rotowire,
                                  "current_availability":
                                      injuries.resolve_availability(rotowire),
                                  "items": rotowire,
                              },
                          }},
        "media_coverage": {"section_title": "What the Media Is Saying",
                           "inputs": {"live_web_research": bucket("media")}},
        "coach_comments": {"section_title": "What the Coaches Are Saying",
                           "inputs": {"live_web_research": bucket("coaches")}},
    }


SYSTEM_PROMPT = (
    "You are a top-tier college football analyst and writer. You write with the confident, "
    "authoritative voice of a national beat analyst and a streak of dark humor where it "
    "fits, and you avoid cheesy, overused sportswriting cliches. You work strictly from the "
    "data you are given — you never invent statistics, players, quotes, sources or news."
)


# ---------------------------------------------------------------------------
# The road ahead: ratings context and remaining-schedule projections
# ---------------------------------------------------------------------------
def _rank_of(rows: list, team: str, field: str) -> tuple:
    """(value, national rank) for one team in a league rating table."""
    scored = [(r.get("team"), r.get(field)) for r in rows or []
              if r.get("team") and r.get(field) is not None]
    scored.sort(key=lambda t: -float(t[1]))
    for i, (name, value) in enumerate(scored, 1):
        if name == team:
            return round(float(value), 2), i
    return None, None


def _ratings_context(league: dict, team_short: str) -> dict:
    sp, sp_rank = _rank_of(league.get("sp"), team_short, "rating")
    fpi, fpi_rank = _rank_of(league.get("fpi"), team_short, "fpi")
    elo, elo_rank = _rank_of(league.get("elo"), team_short, "elo")
    return {
        "sp_plus": {"rating": sp, "national_rank": sp_rank},
        "fpi": {"rating": fpi, "national_rank": fpi_rank},
        "elo": {"rating": elo, "national_rank": elo_rank},
    }


def _road_ahead(league: dict, team_short: str, upcoming: list[dict],
                records: list) -> dict:
    """Every remaining game projected from the league rating tables.

    Reuses the weekly publication's ratings-consensus margin (SP+/FPI/Elo mean with
    home-field advantage) so the team report, the weekly slate and the matchup
    baseline all speak with one voice about the same game.
    """
    import predict
    import weekly

    tables = {"sp": weekly._table(league.get("sp"), "team"),
              "fpi": weekly._table(league.get("fpi"), "team"),
              "elo": weekly._table(league.get("elo"), "team")}
    sp_table = league.get("sp") or []

    games, win_probs = [], []
    for g in upcoming:
        opponent = g.get("opponent")
        if not opponent:
            continue
        neutral = bool(g.get("neutral"))
        if g.get("home"):
            margin = weekly._quick_margin(team_short, opponent, tables, neutral)
        else:
            away_view = weekly._quick_margin(opponent, team_short, tables, neutral)
            margin = -away_view if away_view is not None else None
        prob = (round(predict.win_probability(margin) * 100, 1)
                if margin is not None else None)
        if prob is not None:
            win_probs.append(prob)
        _opp_sp, opp_rank = _rank_of(sp_table, opponent, "rating")
        games.append({
            "week": g.get("week"),
            "date": g.get("start_date"),
            "opponent": opponent,
            "site": "neutral" if neutral else ("home" if g.get("home") else "away"),
            "opponent_sp_rank": opp_rank,
            "projected_margin": margin,
            "win_probability_pct": prob,
        })

    total = (records or [{}])[0].get("total") or {}
    wins_now = int(total.get("wins") or 0)
    losses_now = int(total.get("losses") or 0)
    expected_more = round(sum(win_probs) / 100, 1) if win_probs else None
    return {
        "method": ("Projected margins are the mean of the SP+, FPI and Elo rating "
                   "gaps with home-field advantage applied — the same baseline the "
                   "matchup reports use. Win probabilities follow from that margin."),
        "games": games,
        "current_record": f"{wins_now}-{losses_now}",
        "expected_remaining_wins": expected_more,
        "projected_final_wins": (round(wins_now + expected_more, 1)
                                 if expected_more is not None else None),
        "games_without_a_projection": sum(1 for g in games
                                          if g["projected_margin"] is None),
    }


def _our_record_on_team(team_short: str, year) -> dict:
    """The graded prediction history for this team's games. Fail-soft: the report
    must not care whether the prediction store is reachable."""
    try:
        import predictions
        rows = [r for r in predictions.history(season=year, graded_only=True)
                if team_short in (r.get("home_short"), r.get("away_short"))]
    except Exception as e:
        logging.debug(f"Prediction record unavailable for {team_short}: {e}")
        return {"available": False, "graded_predictions": 0}
    if not rows:
        return {"available": False, "graded_predictions": 0}

    calls = []
    for r in rows:
        as_home = r.get("home_short") == team_short
        predicted = r.get("consensus_margin")
        actual = r.get("actual_margin")
        calls.append({
            "run_date": r.get("run_date"),
            "opponent": r.get("away_short") if as_home else r.get("home_short"),
            "predicted_margin": (round(float(predicted), 1) * (1 if as_home else -1)
                                 if predicted is not None else None),
            "actual_margin": (int(actual) * (1 if as_home else -1)
                              if actual is not None else None),
            "margin_error": r.get("margin_error"),
            "winner_correct": bool(r.get("winner_correct")),
        })
    errors = [c["margin_error"] for c in calls if c["margin_error"] is not None]
    return {
        "available": True,
        "graded_predictions": len(calls),
        "winners_called": sum(1 for c in calls if c["winner_correct"]),
        "mean_abs_margin_error": (round(sum(errors) / len(errors), 2)
                                  if errors else None),
        "calls": calls[:12],
    }


def _build_prompt(ctx, bundle, charts, registry) -> str:
    team = ctx["team_full"]
    sections = "\n".join(
        f"{i}. {title} — {guidance}"
        for i, (title, _key, guidance) in enumerate(SECTIONS, start=1)
    )
    chart_manifest = "\n".join(
        f'- "{c["title"]}" '
        f'({"rendered" if c.get("available") else "NO DATA — placeholder shown, do not describe it"}): '
        f'{c["caption"]}'
        for c in charts
    )
    sources = "\n".join(
        f'[{e["index"]}] {e["title"] or e["publisher"] or e["url"]} — {e["url"]}'
        for e in registry.entries()
    )

    return f"""Write a full-length season report for {team}, {ctx['year']} season.

Report generated: {ctx['now_utc'].strftime('%A, %B %d, %Y at %H:%M UTC')}.

=========================== SECTIONS ===========================
Produce EXACTLY these sections, in this order, and nothing else. No preamble before the
first section, no sign-off after the last.

{sections}

FORMATTING
- Each section heading is a level-2 markdown heading on its own line: "## Section Title".
  Do not number them and do not add any other heading levels above them.
- Open each section with one short line explaining what it covers, and — where it rests on
  a statistic — what that statistic actually measures in plain English.
- Then the key figures (where applicable), then AT LEAST TWO substantial paragraphs of
  analysis. Analysis, not restatement.
- In the schedule section, use TWO markdown tables — completed results (Week, Opponent,
  H/A, Result, Score) and remaining games (Week, Opponent, Site, Projected Margin, Win
  Probability) — each followed by its prose.
- When a section has more than one input bucket, keep them visibly separate with a bolded
  sub-label (e.g. "**Live web research:**" and "**Injury feed:**"). Never blend two
  sources into one undifferentiated list.
- If a section's data is empty or marked no_data, keep the heading and write ONE sentence
  saying no data was available. Nothing else — no speculation, no filler, no apology.

=========================== SOURCING ===========================
Citation markers are PRE-ASSIGNED. Cite with the exact bracketed marker from the list
below, placed inline right after the claim it supports. Use [1] for anything drawn from
the CollegeFootballData statistics and [2] for the injury feed. NEVER invent a marker
that is not on the list, and never write a bare URL. Do NOT write a SOURCES section — one
is appended automatically. Do not mention URLs, retrieval problems or search coverage.
Do not use any fact that is not present in the data below.

PRE-ASSIGNED CITATION MARKERS:
{sources}

=========================== VISUALS ===========================
These charts are rendered into the finished PDF automatically. Reference them by name in
the relevant sections and let your analysis agree with them. Do not attempt to reproduce
them, and never reference a chart marked NO DATA.

{chart_manifest}

=========================== DATA ===========================
{json.dumps(bundle, ensure_ascii=False, default=str)}
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
    """Build one single-team report end to end.

    `report_dir` scopes the output to one account's directory; without it two accounts
    requesting the same team on the same day overwrite each other's PDF.
    """
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

    safe = "".join(ch for ch in team_short if ch.isalnum() or ch in " -_").strip() or "team"
    out_dir = report_dir or config.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    filename = f"team_{safe}_{db.format_friendly_date(today)}.pdf"
    filepath = os.path.join(out_dir, filename)
    tmp_path = filepath + ".building"

    ctx = {
        "team_full": team_full,
        "team_short": team_short,
        "year": year,
        "now_utc": research.build_context(team_full, team_full, team_short, team_short, year)["now_utc"],
    }

    # --- Stage 1: statistics and research, concurrently ----------------------
    step("gather")
    jobs = _jobs(ctx)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cfbd_future = pool.submit(cfbd.fetch_team, cfbd_api_key, year, team_short)
        research_future = pool.submit(research.run_research, openrouter_api_key, ctx, settings, jobs)

        try:
            data = cfbd_future.result()
        except Exception as e:
            logging.exception("CFBD fetch failed")
            raise PipelineError("CFBD data fetch failed", str(e), 502)
        try:
            raw = research_future.result()
        except Exception:
            logging.exception("Research stage failed entirely")
            raw = {}

    auth_failures = data.get("auth_failures") or []
    total = data.get("total_requests") or 0
    if auth_failures and len(auth_failures) >= max(1, total // 2):
        first = auth_failures[0]
        raise PipelineError(
            "CollegeFootballData rejected the API key",
            f"HTTP {first['status']} on {len(auth_failures)}/{total} requests — {first['body']}. "
            f"Run GET /health/cfbd to probe each endpoint individually.",
            502,
        )

    # --- Stage 2: assembly ---------------------------------------------------
    step("assemble")
    stats = data["stats"]
    meta = cfbd.resolve_team_meta(cfbd_api_key, data["teams"], team_short, year)
    games = cfbd.normalize_games(data["games"], team_short)
    played = [g for g in games if g["completed"]]
    upcoming = [g for g in games if not g["completed"]]
    profile = cfbd.scoring_profile(games)

    adv_rows = stats.get("Advanced Team Stats") or []
    percentiles = cfbd.build_percentiles(data["league"]["advanced"], adv_rows[0] if adv_rows else {}, {})

    # Persist what the report's own injury call already found, then read the feed back
    # — refreshing only if no lookup for this team has run inside the TTL.
    injury_bucket = raw.get("injuries") or {}
    injuries.record_findings(team_short, team_full,
                             injury_bucket.get("findings") or [],
                             succeeded=not injury_bucket.get("error"),
                             error=injury_bucket.get("error") or "")
    try:
        rotowire = injuries.ensure_fresh(team_short, team_full, settings)
    except Exception as e:
        logging.warning(f"Injury feed lookup failed: {e}")
        rotowire = []

    registry = research.seed_registry()
    sections = _assemble(raw, rotowire, registry, ctx, settings)

    # The road ahead: ratings context, per-game projections for what remains, and the
    # site's own graded record on this team. All computed here, all fail-soft.
    league = data.get("league") or {}
    ratings_context = _ratings_context(league, team_short)
    outlook = _road_ahead(league, team_short, upcoming, data.get("records") or [])
    our_record = _our_record_on_team(team_short, year)

    # --- Stage 3: visuals ----------------------------------------------------
    step("charts")
    try:
        chart_set = charts_mod.build_team_charts(stats, percentiles, meta, team_short,
                                                 games, outlook=outlook)
    except charts_mod.ChartsUnavailable as e:
        raise PipelineError("Charting library missing on the server", str(e), 500)

    pruned = dict(stats)
    pruned["Player PPA"] = cfbd.prune_player_ppa(stats.get("Player PPA"))

    bundle = {
        "team": {
            "name": team_full,
            "school": team_short,
            "conference": meta.get("conference"),
            "mascot": meta.get("mascot"),
            "season": year,
            "generated_at_utc": ctx["now_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "record": data.get("records") or [],
        "ratings_context": ratings_context,
        "scoring_profile": profile,
        "schedule": {"completed": played, "upcoming": upcoming},
        "remaining_schedule_outlook": outlook,
        "our_prediction_record_on_this_team": our_record,
        "transfer_portal": cfbd.portal_moves(data.get("portal"), team_short),
        "statistics_cfbd": pruned,
        "national_percentiles": percentiles,
        "news_and_research": sections,
    }

    # --- Stage 4: synthesis --------------------------------------------------
    step("write")
    try:
        result = report_mod.generate(
            openrouter_api_key, ctx, bundle, chart_set, registry, settings,
            prompt=_build_prompt(ctx, bundle, chart_set, registry),
            system_prompt=SYSTEM_PROMPT,
        )
    except Exception as e:
        logging.exception("Report generation failed")
        detail = str(e)
        body = getattr(e, "body", None)
        if body and str(body) not in detail:
            detail = f"{detail} | upstream: {body}"
        raise PipelineError("Report model request failed", detail[:500], 502)

    # --- Stage 5: render -----------------------------------------------------
    step("pdf")
    usage = result["usage"]
    research_usage = [(r.get("usage") or {}) for r in raw.values() if isinstance(r, dict)]
    with_data = sum(
        1 for s in sections.values()
        if any(not b.get("no_data", True) for b in s["inputs"].values())
    )

    meta_lines = [
        f"Research: {settings['research_model']} via OpenRouter — {len(jobs)} live web "
        f"searches, {with_data}/{len(sections)} sections with findings "
        f"({sum(u.get('input_tokens') or 0 for u in research_usage)} in / "
        f"{sum(u.get('output_tokens') or 0 for u in research_usage)} out tokens).",
        f"Report: {result['model']} via OpenRouter — {usage.get('input_tokens') or 'N/A'} input "
        f"tokens / {usage.get('output_tokens') or 'N/A'} output tokens.",
        f"Statistics: CollegeFootballData ({year} season). Visuals generated procedurally.",
        f"Sources cited: {len(registry)}. Generation time: {int(time.time() - started)}s.",
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
        title=f"{team_full} Season Report",
        banner="College Football Team Report",
        include_sources=bool(settings.get("include_sources", 1)),
        include_generation_details=bool(settings.get("include_generation_details", 1)),
    )

    try:
        render.write_pdf(html, tmp_path,
                         footer_subject=f"{team_full} — {year}",
                         footer_brand="Team Report")
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
                opacity=float(settings.get('watermark_opacity',
                                           config.WATERMARK_OPACITY)),
                scale=float(settings.get('watermark_scale',
                                         config.WATERMARK_SCALE)),
            )
        except Exception as e:
            logging.warning(f"Watermark step failed; delivering report without watermark: {e}")

    os.replace(tmp_path, filepath)

    elapsed = int(time.time() - started)
    step("done")
    logging.info(f"Team report {filename} generated in {elapsed}s ({len(registry)} sources).")

    return {
        "filename": filename,
        "seconds": elapsed,
        "sources": len(registry),
        "sections_with_research": with_data,
        "games_played": len(played),
        "games_upcoming": len(upcoming),
        "record": profile,
    }
