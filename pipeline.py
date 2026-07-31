"""The report generation pipeline, decoupled from Flask.

Runs in a background worker thread, so nothing in here may touch the request context.
Progress is reported back through a callback the job manager supplies.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import cfbd
import charts as charts_mod
import config
import db
import predict
import render
import report as report_mod
import research


class PipelineError(RuntimeError):
    """Failure with a message safe to surface to the frontend."""

    def __init__(self, message: str, detail: str = "", status: int = 500):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status


# (percent complete, human-readable stage) checkpoints, in order.
STAGES = {
    "start": (5, "Starting up"),
    "gather": (12, "Pulling CFBD statistics and running 8 live web searches"),
    "assemble": (48, "Compiling sources and computing the projection baseline"),
    "charts": (58, "Rendering charts"),
    "write": (68, "Writing the report"),
    "pdf": (92, "Building the PDF"),
    "done": (100, "Complete"),
}


def _noop(_stage, _pct, _label):
    pass


def generate(
    *,
    home_full: str,
    away_full: str,
    home_short: str,
    away_short: str,
    year: int | None = None,
    kickoff: str | None = None,
    progress=None,
) -> dict:
    """Build one matchup report end to end. Returns a result summary dict."""
    progress = progress or _noop
    started = time.time()
    current = {"stage": "start", "label": "Starting up"}

    def step(key):
        pct, label = STAGES[key]
        current["stage"], current["label"] = key, label
        progress(key, pct, label)

    generate.current_stage = current  # read by the job manager when something escapes
    step("start")

    cfbd_api_key = db.resolve_cfbd_key()
    openrouter_api_key = db.resolve_openrouter_key()
    missing = [n for n, v in (("CFBD", cfbd_api_key), ("OpenRouter", openrouter_api_key)) if not v]
    if missing:
        raise PipelineError(
            f"Missing required API key(s): {', '.join(missing)}",
            "Add an 'openrouter' row to the API_KEYS table or set OPENROUTER_API_KEY.",
        )

    today = datetime.now()
    if not year:
        year = cfbd.season_year(today)

    filename = f"{home_short}_{away_short}_{db.format_friendly_date(today)}.pdf"
    filepath = os.path.join(config.REPORTS_DIR, filename)
    # Build to a temp path and swap at the end, so any existing report for this matchup
    # stays downloadable for the entire multi-minute run.
    tmp_path = filepath + ".building"

    ctx = research.build_context(home_full, away_full, home_short, away_short, year, kickoff=kickoff)

    # --- Stage 1: statistics and live research, concurrently -----------------
    step("gather")
    with ThreadPoolExecutor(max_workers=2) as pool:
        cfbd_future = pool.submit(cfbd.fetch_all, cfbd_api_key, year, home_short, away_short)
        research_future = pool.submit(research.run_research, openrouter_api_key, ctx)

        try:
            cfbd_data = cfbd_future.result()
        except Exception as e:
            logging.exception("CFBD fetch failed")
            raise PipelineError("CFBD data fetch failed", str(e), 502)

        try:
            research_raw = research_future.result()
        except Exception:
            logging.exception("Research stage failed entirely")
            research_raw = {}

    auth_failures = cfbd_data.get("auth_failures") or []
    total_requests = cfbd_data.get("total_requests") or 0
    if auth_failures and len(auth_failures) >= max(1, total_requests // 2):
        first = auth_failures[0]
        raise PipelineError(
            "CollegeFootballData rejected the API key",
            f"HTTP {first['status']} on {len(auth_failures)}/{total_requests} requests "
            f"— {first['body']}. Check the 'CFD' row in the API_KEYS table "
            f"(or CFBD_API_KEY) and confirm the key at collegefootballdata.com/key.",
            502,
        )

    stats = cfbd_data["stats"]

    # --- Stage 2: assembly ---------------------------------------------------
    step("assemble")
    home_meta = cfbd.team_meta(cfbd_data["teams"], home_short)
    away_meta = cfbd.team_meta(cfbd_data["teams"], away_short)

    home_games = cfbd.normalize_games(cfbd_data["games"]["teamA"], home_short)
    away_games = cfbd.normalize_games(cfbd_data["games"]["teamB"], away_short)
    home_profile = cfbd.scoring_profile(home_games)
    away_profile = cfbd.scoring_profile(away_games)
    market = cfbd.find_matchup_line(cfbd_data["lines"], home_short, away_short)

    advanced = stats.get("Advanced Team Stats") or {}
    home_adv = (advanced.get("teamA") or [{}])[0] if advanced.get("teamA") else {}
    away_adv = (advanced.get("teamB") or [{}])[0] if advanced.get("teamB") else {}
    percentiles = cfbd.build_percentiles(cfbd_data["league"]["advanced"], home_adv, away_adv)

    try:
        rotowire = {
            "home": db.fetch_rotowire_for_team(home_short, home_full),
            "away": db.fetch_rotowire_for_team(away_short, away_full),
        }
    except Exception as e:
        logging.warning(f"Rotowire lookup failed: {e}")
        rotowire = {"home": [], "away": []}

    registry = research.seed_registry()
    sections = research.assemble_sections(research_raw, rotowire, registry, ctx)
    baseline = predict.build_baseline(stats, home_profile, away_profile, market, home_short, away_short)

    # --- Stage 3: visuals ----------------------------------------------------
    step("charts")
    chart_set = charts_mod.build_all(
        stats, percentiles, baseline, home_meta, away_meta, home_short, away_short
    )

    bundle = {
        "matchup": {
            "home_team": home_full,
            "home_short": home_short,
            "home_conference": home_meta.get("conference"),
            "away_team": away_full,
            "away_short": away_short,
            "away_conference": away_meta.get("conference"),
            "season": year,
            "kickoff": kickoff or "",
            "generated_at_utc": ctx["now_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "statistics_cfbd": cfbd.prune_for_prompt(stats),
        "national_percentiles": percentiles,
        "season_results": {"home": home_games, "away": away_games},
        "scoring_profiles": {"home": home_profile, "away": away_profile},
        "betting_market": market,
        "statistical_baseline": baseline,
        "news_and_research": sections,
    }

    # --- Stage 4: synthesis --------------------------------------------------
    step("write")
    try:
        result = report_mod.generate(openrouter_api_key, ctx, bundle, chart_set, registry)
    except Exception as e:
        logging.exception("Report generation failed")
        raise PipelineError("Report model request failed", str(getattr(e, "body", None) or e)[:500], 502)

    usage = result["usage"]

    # --- Stage 5: render -----------------------------------------------------
    step("pdf")
    research_usage = [(r.get("usage") or {}) for r in research_raw.values() if isinstance(r, dict)]
    research_in = sum(u.get("input_tokens") or 0 for u in research_usage)
    research_out = sum(u.get("output_tokens") or 0 for u in research_usage)
    sections_with_data = sum(
        1 for s in sections.values()
        if any(not b.get("no_data", True) for b in s["inputs"].values())
    )

    meta_lines = [
        f"Research: {config.OPENROUTER_RESEARCH_MODEL} via OpenRouter — "
        f"{len(research.RESEARCH_JOBS)} live web searches, "
        f"{sections_with_data}/{len(sections)} sections with findings "
        f"({research_in} in / {research_out} out tokens).",
        f"Report: {result['model']} via OpenRouter — "
        f"{usage.get('input_tokens') or 'N/A'} input tokens / "
        f"{usage.get('output_tokens') or 'N/A'} output tokens.",
        f"Statistics: CollegeFootballData ({year} season). "
        f"Visuals generated procedurally from those feeds.",
        f"Sources cited: {len(registry)}. Generation time: {int(time.time() - started)}s.",
    ]

    html_content = render.build_html(
        home_full=home_full,
        away_full=away_full,
        year=year,
        home_logo=home_meta.get("logo", ""),
        away_logo=away_meta.get("logo", ""),
        report_created=f"{db.format_friendly_date(today)} {today.strftime('%I:%M %p')}",
        report_markdown=result["text"],
        charts=chart_set,
        registry=registry,
        meta_lines=meta_lines,
    )

    try:
        render.write_pdf(html_content, tmp_path)
    except ImportError:
        raise PipelineError("PDF generation library not installed on server.", "", 500)
    except Exception as e:
        logging.error(f"PDF generation failed: {e}")
        raise PipelineError("PDF generation failed", str(e), 500)

    if os.path.exists(config.WATERMARK_PATH):
        try:
            render.add_pdf_watermark(tmp_path, config.WATERMARK_PATH)
        except Exception as e:
            logging.warning(f"Watermark step failed; delivering report without watermark: {e}")
    else:
        logging.warning(f"Watermark image not found at {config.WATERMARK_PATH}; skipping watermark.")

    # Swap in the finished file, then retire any older report for this matchup.
    os.replace(tmp_path, filepath)
    cleanup_old_reports(home_short, away_short, keep_filename=filename)

    elapsed = int(time.time() - started)
    step("done")
    logging.info(
        f"Report {filename} generated in {elapsed}s "
        f"({len(result['text'])} chars, {len(registry)} sources)."
    )

    return {
        "filename": filename,
        "seconds": elapsed,
        "sources": len(registry),
        "sections_with_research": sections_with_data,
        "projected_score": baseline.get("projected_score"),
        "baseline_margin": baseline.get("consensus_margin"),
        "home_win_probability": baseline.get("home_win_probability"),
    }


def cleanup_old_reports(home_short: str, away_short: str, keep_filename: str | None = None) -> None:
    import glob

    pattern = os.path.join(config.REPORTS_DIR, f"{home_short}_{away_short}_*.pdf")
    for path in glob.glob(pattern):
        if not keep_filename or os.path.basename(path) != keep_filename:
            try:
                os.remove(path)
            except Exception as e:
                logging.warning(f"Could not delete old report {path}: {e}")
