"""The prediction record, turned into reports.

prediction_audit — internal. Names the report models, compares their graded accuracy,
and shows how predictions move as game day approaches. For the operator's eyes: this
is the report that decides which model earns its cost.

prediction_review — public. The same performance story with the machinery redacted:
no model names, no component systems, no blend weights. Just the prediction analysis —
accuracy, the days-out curve, best and worst calls.

Both grade any pending predictions first, so the numbers are as current as the games.
"""

import json
import logging
import os
import time
from datetime import datetime

import accounts
import charts as charts_mod
import config
import db
import predictions
import render
import report as report_mod
import research
from pipeline import PipelineError

STAGES = {
    "start":  (5,   "Starting up"),
    "grade":  (20,  "Grading pending predictions against final scores"),
    "math":   (45,  "Aggregating the prediction record"),
    "charts": (60,  "Rendering charts"),
    "write":  (70,  "Writing the report"),
    "pdf":    (92,  "Building the PDF"),
    "done":   (100, "Complete"),
}

AUDIT_SECTIONS = [
    ("Scoreboard", "The overall record: predictions made, graded, mean absolute margin "
                   "error, straight-up winner percentage, against-the-spread record, "
                   "totals record. State it plainly."),
    ("Model Comparison", "Every report model's graded record side by side, as a table: "
                         "predictions, MAE, winner %, ATS. Say which model is earning "
                         "its cost and which is not, and how confident the sample size "
                         "allows you to be."),
    ("The Days-Out Curve", "Does the prediction get better as kickoff approaches? Read "
                           "the bucketed error curve and say what it means for WHEN "
                           "reports should be generated."),
    ("Prediction Trajectories", "Games predicted on multiple days: how the margin and "
                                "win probability moved run by run, and whether the "
                                "movement went toward or away from the final result."),
    ("Best and Worst Calls", "The largest graded hits and misses, each in a sentence "
                             "with its numbers."),
    ("Recommendations", "Concrete operating changes the record supports: model choice, "
                        "generation timing, market-blend weighting. Only claims the "
                        "data can carry."),
]

REVIEW_SECTIONS = [
    ("The Record", "The overall prediction record: games predicted, graded, mean "
                   "margin error, winner percentage, against-the-spread performance. "
                   "State it plainly — including where it is unimpressive."),
    ("How Predictions Sharpen", "How accuracy changes as game day approaches, from the "
                                "bucketed curve, and what that means for readers about "
                                "when a projection is most trustworthy."),
    ("Signature Calls", "The best calls in the record — and the worst, presented with "
                        "equal honesty. Each with its numbers."),
    ("Where the Projections Struggle", "The situations the graded record shows are "
                                       "hardest to project, whatever they are."),
    ("Reading These Numbers", "How a reader should and should not use this record: "
                              "analysis for entertainment, not advice; past accuracy "
                              "guarantees nothing."),
]

SYSTEM_PROMPT = (
    "You are an analyst auditing a college football prediction record from verified "
    "grading data. Every number comes from the supplied data; never invent one. Be "
    "candid about weaknesses — an audit that flatters is worthless."
)


def _redact(rows: list[dict]) -> list[dict]:
    """The public review never sees model names or internal machinery."""
    out = []
    for r in rows:
        r = dict(r)
        r.pop('report_model', None)
        r.pop('baseline_json', None)
        r.pop('account_id', None)
        r.pop('report_filename', None)
        out.append(r)
    return out


def _slim(rows: list[dict], limit: int = 120, public: bool = False) -> list[dict]:
    keep = ('run_date', 'season', 'week', 'home_short', 'away_short', 'report_model',
            'consensus_margin', 'market_margin', 'projected_total', 'market_total',
            'home_win_probability', 'graded', 'actual_home', 'actual_away',
            'actual_margin', 'margin_error', 'winner_correct', 'ats_result',
            'total_result', 'game_date')
    if public:
        # Not even the KEY may appear in the public prompt — a null 'report_model'
        # still tells a reader that models exist and vary.
        keep = tuple(k for k in keep if k != 'report_model')
    return [{k: r.get(k) for k in keep} for r in rows[:limit]]


def generate(kind: str, *, year=None, settings=None, watermark=None,
             report_dir=None, progress=None) -> dict:
    if kind not in ('audit', 'review'):
        raise PipelineError(f"Unknown prediction report kind '{kind}'")
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

    openrouter_api_key = db.resolve_openrouter_key()
    if not openrouter_api_key:
        raise PipelineError("Missing required API key(s): OpenRouter")

    step("grade")
    cfbd_api_key = db.resolve_cfbd_key()
    if cfbd_api_key:
        try:
            predictions.grade_pending(cfbd_api_key)
        except Exception as e:
            logging.warning(f"Grading pass failed; reporting on what is graded: {e}")

    step("math")
    try:
        comparison = predictions.model_comparison(season=year)
        curve = predictions.days_out_curve(season=year)
        trajectories = predictions.per_game_trajectories(season=year)
        graded = predictions.history(season=year, graded_only=True)
        everything = predictions.history(season=year)
    except Exception as e:
        raise PipelineError("The prediction record is unavailable",
                            f"{e.__class__.__name__}: {e}", 503)
    if not everything:
        raise PipelineError(
            "No predictions recorded yet",
            "Generate matchup reports first — each one files its prediction here.", 404)

    step("charts")
    try:
        chart_set = charts_mod.build_prediction_charts(
            kind, curve=curve, by_model=comparison['by_model'], graded_rows=graded)
    except charts_mod.ChartsUnavailable as e:
        raise PipelineError("Charting library missing on the server", str(e), 500)

    if kind == 'audit':
        sections = AUDIT_SECTIONS
        noun = 'Prediction Audit'
        bundle = {
            'overall': comparison['overall'],
            'by_model': comparison['by_model'],
            'days_out_curve': curve,
            'trajectories': trajectories,
            'graded_predictions': _slim(graded),
            'ungraded_pending': len(everything) - len(graded),
        }
        extra_rules = ("- This is an INTERNAL audit: name the report models and "
                       "compare them directly.")
    else:
        sections = REVIEW_SECTIONS
        noun = 'Prediction Review'
        bundle = {
            'overall': comparison['overall'],
            'days_out_curve': curve,
            'graded_predictions': _slim(_redact(graded), public=True),
            'ungraded_pending': len(everything) - len(graded),
        }
        extra_rules = (
            "- This review is PUBLIC. Never mention models, systems, components, "
            "blends, weights, vendors or anything about HOW predictions are produced "
            "— only the predictions themselves and their results. Close by reminding "
            "the reader this is analysis for entertainment, not betting advice.")

    registry = research.SourceRegistry()
    registry.add("https://collegefootballdata.com", "College Football Data API",
                 "CollegeFootballData")

    section_text = "\n".join(f"{i}. {t} — {g}" for i, (t, g) in enumerate(sections, 1))
    charts_text = "\n".join(f'- "{c["title"]}" (rendered): {c["caption"]}'
                            for c in chart_set)
    scope = f"the {year} season" if year else "all recorded seasons"
    prompt = f"""Write the complete {noun} covering {scope}.

Produce EXACTLY these sections, in this order, and nothing else.

{section_text}

FORMAT RULES:
- Level-2 markdown headings ("## Section Title"); markdown tables for comparisons.
- EVERY number comes from the DATA below — never invent, estimate or recall one.
- Predictions were deduplicated to one per game per day (latest kept), and grading is
  against final scores from the game record [1].
{extra_rules}

RENDERED CHARTS (already placed in the report):
{charts_text}

PRE-ASSIGNED CITATION MARKERS:
[1] CollegeFootballData final scores, used to grade every prediction.

DATA:
{json.dumps(bundle, separators=(',', ':'), default=str)}
"""

    step("write")
    ctx = {'home_team': noun, 'away_team': '', 'year': year or ''}
    try:
        result = report_mod.generate(
            openrouter_api_key, ctx, bundle, chart_set, registry, settings,
            prompt=prompt, system_prompt=SYSTEM_PROMPT)
    except Exception as e:
        logging.exception("Prediction report generation failed")
        raise PipelineError("Report model request failed", str(e)[:500], 502)

    step("pdf")
    today = datetime.now()
    prefix = 'predaudit' if kind == 'audit' else 'predreview'
    scope_name = f"{year}" if year else "All Seasons"
    filename = f"{prefix}_{scope_name}_{db.format_friendly_date(today)}.pdf"
    out_dir = report_dir or config.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    filepath = os.path.join(out_dir, filename)
    tmp_path = filepath + ".building"

    usage_stats = result["usage"]
    overall = comparison['overall']
    meta_lines = [
        f"Record: {overall['predictions']} predictions, {overall['graded']} graded "
        f"(MAE {overall['mean_abs_error']}, winners {overall['winner_pct']}%, "
        f"ATS {overall['ats_record']}). Grading source: CollegeFootballData finals.",
        f"Report: {result['model']} via OpenRouter — "
        f"{usage_stats.get('input_tokens') or 'N/A'} input / "
        f"{usage_stats.get('output_tokens') or 'N/A'} output tokens.",
        f"Generation time: {int(time.time() - started)}s.",
    ]

    html = render.build_html(
        home_full=noun, away_full='', year=year or today.year,
        home_logo='', away_logo='',
        report_created=f"{db.format_friendly_date(today)} {today.strftime('%I:%M %p')}",
        report_markdown=result["text"], charts=chart_set, registry=registry,
        meta_lines=meta_lines,
        title=f"{noun} — {scope_name}",
        banner=f"College Football {noun}",
        include_sources=bool(settings.get("include_sources", 1)),
        include_generation_details=bool(settings.get("include_generation_details", 1)),
    )
    try:
        render.write_pdf(html, tmp_path, footer_subject=f"{noun} — {scope_name}",
                         footer_brand=noun)
    except ImportError:
        raise PipelineError("PDF generation library not installed on server.", "", 500)
    except Exception as e:
        raise PipelineError("PDF generation failed", str(e), 500)

    stamp = watermark or config.WATERMARK_PATH
    if os.path.exists(stamp):
        try:
            render.add_pdf_watermark(
                tmp_path, stamp,
                opacity=float(settings.get("watermark_opacity", config.WATERMARK_OPACITY)),
                scale=float(settings.get("watermark_scale", config.WATERMARK_SCALE)))
        except Exception as e:
            logging.warning(f"Watermark failed; shipping unstamped: {e}")

    os.replace(tmp_path, filepath)
    elapsed = int(time.time() - started)
    step("done")
    logging.info(f"{noun} {filename} generated in {elapsed}s "
                 f"({overall['graded']} graded predictions).")
    return {"filename": filename, "seconds": elapsed,
            "predictions": overall['predictions'], "graded": overall['graded']}
