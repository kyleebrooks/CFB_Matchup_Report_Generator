"""The two self-generating weekly publications.

weekly_preview — "The Week N Card": every FBS game in one report, with a quick model
margin for each built from the league rating tables, compared against the market line.
weekly_wrap — "What Week N Told Us": every final, the surprises, the ratings movers,
and what the results actually mean.

Both are data-only (no web research) and cheap: the per-game math is computed here,
not by the model — one synthesis call writes the prose around it.
"""

import logging
import os
import time
from datetime import datetime, timezone

import accounts
import cfbd
import charts as charts_mod
import config
import db
import predict
import render
import report as report_mod
import research
from pipeline import PipelineError

STAGES = {
    "start":  (5,   "Starting up"),
    "gather": (15,  "Pulling the week's games, lines and rating tables"),
    "math":   (45,  "Computing per-game projections"),
    "charts": (60,  "Rendering charts"),
    "write":  (70,  "Writing the report"),
    "pdf":    (92,  "Building the PDF"),
    "done":   (100, "Complete"),
}

PREVIEW_SECTIONS = [
    ("Week Overview", "The shape of the week: how many games, the headliners, what is "
                      "at stake nationally, and where the model and the market disagree "
                      "most. Set the table in a few tight paragraphs."),
    ("Games of the Week", "The five most consequential games (they are flagged in the "
                          "data). For each: the stakes, the statistical edge, the model "
                          "margin against the market line, and what decides it."),
    ("The Full Slate Board", "EVERY game in the data, as one markdown table ordered by "
                             "date then interest: matchup, model margin, market line, "
                             "edge, projected total. Keep rows terse."),
    ("Upset Watch", "Games where the model or the ratings gap says the favorite is "
                    "weaker than the market believes, plus live underdogs. Only games "
                    "supported by the numbers in the data."),
    ("Ranked Teams on Alert", "Ranked teams (flagged in the data) whose game carries "
                              "either a thin model margin or a hostile setting."),
    ("What to Watch, Day by Day", "A viewing guide grouped by date, closing with the "
                                  "one storyline to follow into next week."),
]

WRAP_SECTIONS = [
    ("The Weekend in Brief", "What actually happened this week, in three or four "
                             "paragraphs: the headline results, the theme of the week, "
                             "and the national picture after it."),
    ("Biggest Surprises", "Finals that defied the pregame Elo gap, and games where the "
                          "postgame win probability says the losing side deserved "
                          "better. Every claim anchored to those two numbers."),
    ("Ratings Movers", "The teams whose Elo moved most, up and down, with the result "
                       "that moved it. Use the pre/postgame Elo in the data."),
    ("Escapes and Blowouts", "Favorites that barely survived, and the week's most "
                             "lopsided results."),
    ("The Best Games", "The excitement-index leaders — which finals were worth the "
                       "watch, and why the index rated them."),
    ("Overreaction Check", "Two or three results the public will overrate or "
                           "underrate, judged strictly against the underlying numbers "
                           "in the data."),
    ("What It Sets Up", "What next week now means: the stakes created or destroyed by "
                        "this week's results."),
]

SYSTEM_PROMPT = (
    "You are a college football analyst writing a weekly publication from verified "
    "game and ratings data. Every number you print comes from the supplied data — "
    "never invent, estimate or recall one. Plain, confident, readable prose."
)


def _table(rows, team_field):
    return {r.get(team_field): r for r in rows or [] if r.get(team_field)}


def _quick_margin(home, away, tables, neutral):
    """Per-game ratings consensus, mirroring the matchup report's baseline math."""
    hfa = 0.0 if neutral else config.HOME_FIELD_ADVANTAGE
    margins = []
    sp_h, sp_a = tables['sp'].get(home), tables['sp'].get(away)
    if sp_h and sp_a and sp_h.get('rating') is not None and sp_a.get('rating') is not None:
        margins.append(float(sp_h['rating']) - float(sp_a['rating']) + hfa)
    fpi_h, fpi_a = tables['fpi'].get(home), tables['fpi'].get(away)
    if fpi_h and fpi_a and fpi_h.get('fpi') is not None and fpi_a.get('fpi') is not None:
        margins.append(float(fpi_h['fpi']) - float(fpi_a['fpi']) + hfa)
    elo_h, elo_a = tables['elo'].get(home), tables['elo'].get(away)
    if elo_h and elo_a and elo_h.get('elo') is not None and elo_a.get('elo') is not None:
        margins.append((float(elo_h['elo']) - float(elo_a['elo'])) / config.ELO_POINTS_PER_MARGIN
                       + hfa)
    return round(sum(margins) / len(margins), 1) if margins else None


def _week_lines(api_key, year, week, season_type, errors):
    rows = cfbd._get(api_key, '/lines',
                     {'year': year, 'week': week, 'seasonType': season_type},
                     f'Lines (week {week})', errors) or []
    out = {}
    for row in rows:
        gid = row.get('id') or row.get('gameId')
        lines = row.get('lines') or []
        spreads = [l.get('spread') for l in lines if l.get('spread') is not None]
        totals = [l.get('overUnder') for l in lines if l.get('overUnder') is not None]
        if gid and (spreads or totals):
            # CFBD spreads are home-negative-when-favored; flip to home margin.
            margin = round(-sum(float(s) for s in spreads) / len(spreads), 2) if spreads else None
            total = round(sum(float(t) for t in totals) / len(totals), 2) if totals else None
            out[gid] = {'market_margin': margin, 'market_total': total,
                        'providers': sorted({l.get('provider') for l in lines
                                             if l.get('provider')})}
    return out


def _ap_top25(api_key, year, week, season_type, errors) -> dict:
    rows = cfbd._get(api_key, '/rankings',
                     {'year': year, 'week': week, 'seasonType': season_type},
                     f'Rankings (week {week})', errors) or []
    for entry in rows:
        for poll in entry.get('polls') or []:
            if 'AP' in (poll.get('poll') or ''):
                return {r.get('school'): r.get('rank')
                        for r in poll.get('ranks') or [] if r.get('school')}
    return {}


def build_week_data(api_key, year=None, week=None, season_type=None, *,
                    completed: bool) -> dict:
    """Everything either weekly report needs for one week."""
    now = datetime.now(timezone.utc)
    errors: list[dict] = []
    if year is None:
        year = cfbd.season_year(now)
    weeks = cfbd.calendar_weeks(api_key, year, errors)
    if week is None:
        idx = cfbd._week_around(weeks, now)
        if idx is not None and weeks:
            # The wrap looks back at the most recent finished week; the preview looks
            # at the week in progress (or next).
            if completed and idx > 0 and weeks[idx]['_start'] <= now:
                candidate = weeks[idx]
                prior = weeks[idx - 1]
                week, season_type = ((candidate['week'], candidate['season_type'])
                                     if now > candidate['_end'] else
                                     (prior['week'], prior['season_type']))
            else:
                week, season_type = weeks[idx]['week'], weeks[idx]['season_type']
        else:
            week = 1
    if not season_type:
        match = next((w for w in weeks if w['week'] == week), None)
        season_type = match['season_type'] if match else 'regular'

    games = cfbd.games_for(api_key, year, week, season_type, errors)
    tables = {
        'sp': _table(cfbd._get(api_key, '/ratings/sp', {'year': year}, 'SP', errors), 'team'),
        'fpi': _table(cfbd._get(api_key, '/ratings/fpi', {'year': year}, 'FPI', errors), 'team'),
        'elo': _table(cfbd._get(api_key, '/ratings/elo', {'year': year}, 'Elo', errors), 'team'),
    }
    lines = _week_lines(api_key, year, week, season_type, errors)
    ranks = _ap_top25(api_key, year, week, season_type, errors)

    rows = []
    for g in games:
        if completed and not g['completed']:
            continue
        if not completed and g['completed']:
            continue
        market = lines.get(g['id']) or {}
        model = _quick_margin(g['home'], g['away'], tables, g['neutral_site'])
        market_margin = market.get('market_margin')
        edge = (round(model - market_margin, 1)
                if model is not None and market_margin is not None else None)
        sp_h = (tables['sp'].get(g['home']) or {}).get('rating')
        sp_a = (tables['sp'].get(g['away']) or {}).get('rating')
        interest = (float(sp_h or -30) + float(sp_a or -30)
                    + (25 if g['home'] in ranks or g['away'] in ranks else 0))
        row = {
            'game_id': g['id'], 'start': g['start'], 'venue': g['venue'],
            'neutral_site': g['neutral_site'],
            'home': g['home'], 'away': g['away'],
            'home_rank': ranks.get(g['home']), 'away_rank': ranks.get(g['away']),
            'home_conference': g['home_conference'],
            'away_conference': g['away_conference'],
            'model_margin_home': model,
            'market_margin_home': market_margin,
            'market_total': market.get('market_total'),
            'model_vs_market_edge': edge,
            'home_win_probability': (round(predict.win_probability(model) * 100, 1)
                                     if model is not None else None),
            '_interest': round(interest, 1),
        }
        if completed:
            row.update({
                'home_points': g['home_points'], 'away_points': g['away_points'],
                'margin': (g['home_points'] - g['away_points'])
                          if g['home_points'] is not None and g['away_points'] is not None
                          else None,
            })
        rows.append(row)

    rows.sort(key=lambda r: -r['_interest'])
    for i, r in enumerate(rows):
        r['headliner'] = i < 5
    rows.sort(key=lambda r: (r['start'] or '', r['home']))

    return {'season': year, 'week': week, 'season_type': season_type,
            'games': rows, 'top25': ranks, 'errors': errors,
            'auth_failures': [e for e in errors if e['status'] in (401, 403)]}


def _raw_completed(api_key, year, week, season_type, errors):
    """The raw /games rows for a finished week — the wrap needs Elo and excitement."""
    rows = cfbd._get(api_key, '/games',
                     {'year': year, 'week': week, 'seasonType': season_type,
                      'classification': 'fbs'}, f'Games raw (week {week})', errors) or []
    out = []
    for g in rows:
        if not g.get('completed'):
            continue
        out.append({
            'game_id': g.get('id'), 'start': g.get('startDate'),
            'home': g.get('homeTeam'), 'away': g.get('awayTeam'),
            'home_points': g.get('homePoints'), 'away_points': g.get('awayPoints'),
            'home_pregame_elo': g.get('homePregameElo'),
            'home_postgame_elo': g.get('homePostgameElo'),
            'away_pregame_elo': g.get('awayPregameElo'),
            'away_postgame_elo': g.get('awayPostgameElo'),
            'home_postgame_win_probability': g.get('homePostgameWinProbability'),
            'excitement_index': g.get('excitementIndex'),
        })
    return out


def _generate(kind: str, *, year=None, week=None, settings=None, watermark=None,
              report_dir=None, progress=None) -> dict:
    progress = progress or (lambda *a: None)
    settings = settings or accounts.effective_settings(None)
    started = time.time()
    current = {"stage": "start", "label": "Starting up"}

    def step(key):
        pct, label = STAGES[key]
        current["stage"], current["label"] = key, label
        progress(key, pct, label)

    _generate.current_stage = current
    step("start")

    cfbd_api_key = db.resolve_cfbd_key()
    openrouter_api_key = db.resolve_openrouter_key()
    missing = [n for n, v in (("CFBD", cfbd_api_key), ("OpenRouter", openrouter_api_key)) if not v]
    if missing:
        raise PipelineError(f"Missing required API key(s): {', '.join(missing)}")

    step("gather")
    completed = kind == 'wrap'
    data = build_week_data(cfbd_api_key, year, week, completed=completed)
    if data['auth_failures'] and len(data['auth_failures']) >= 3:
        first = data['auth_failures'][0]
        raise PipelineError("CollegeFootballData rejected the API key",
                            f"HTTP {first['status']} — {first['body']}", 502)
    if not data['games']:
        which = 'finals' if completed else 'scheduled games'
        raise PipelineError(
            f"No {which} found for {data['season']} week {data['week']}",
            "Pick a different week, or wait for the schedule/results to post.", 404)

    step("math")
    extras = {}
    if completed:
        errors: list[dict] = []
        extras['finals_detail'] = _raw_completed(
            cfbd_api_key, data['season'], data['week'], data['season_type'], errors)

    step("charts")
    try:
        chart_set = charts_mod.build_weekly_charts(kind, data, extras)
    except charts_mod.ChartsUnavailable as e:
        raise PipelineError("Charting library missing on the server", str(e), 500)

    registry = research.SourceRegistry()
    registry.add("https://collegefootballdata.com", "College Football Data API",
                 "CollegeFootballData")

    sections = PREVIEW_SECTIONS if kind == 'preview' else WRAP_SECTIONS
    section_text = "\n".join(f"{i}. {t} — {g}" for i, (t, g) in enumerate(sections, 1))
    charts_text = "\n".join(f'- "{c["title"]}" (rendered): {c["caption"]}'
                            for c in chart_set)
    import json as _json
    bundle = {'week': {k: data[k] for k in ('season', 'week', 'season_type')},
              'games': [{k: v for k, v in g.items() if not k.startswith('_')}
                        for g in data['games']],
              'ap_top25': data['top25'], **extras}
    label = f"{data['season']} Week {data['week']}"
    noun = 'Weekly Slate Preview' if kind == 'preview' else 'Weekly Wrap'
    prompt = f"""Write the complete {noun} for {label} of the college football season.

Produce EXACTLY these sections, in this order, and nothing else. No preamble, no
sign-off.

{section_text}

FORMAT RULES:
- Level-2 markdown headings ("## Section Title"), one short opening line per section.
- Markdown tables where the section calls for them; keep rows terse.
- Cite the data source with the pre-assigned marker [1] once per section at most.
- EVERY number comes from the DATA below — never invent, estimate or recall one. No
  injuries, no news, no quotes: this publication is built from the data alone.

RENDERED CHARTS (already placed in the report — reference, do not re-describe):
{charts_text}

PRE-ASSIGNED CITATION MARKERS:
[1] CollegeFootballData (games, rating tables, betting lines, rankings).

DATA:
{_json.dumps(bundle, separators=(',', ':'), default=str)}
"""

    step("write")
    ctx = {'home_team': label, 'away_team': '', 'year': data['season']}
    try:
        result = report_mod.generate(
            openrouter_api_key, ctx, bundle, chart_set, registry, settings,
            prompt=prompt, system_prompt=SYSTEM_PROMPT)
    except Exception as e:
        logging.exception("Weekly report generation failed")
        raise PipelineError("Report model request failed", str(e)[:500], 502)

    step("pdf")
    today = datetime.now()
    prefix = 'slate' if kind == 'preview' else 'wrap'
    filename = (f"{prefix}_{data['season']} Week {data['week']}_"
                f"{db.format_friendly_date(today)}.pdf")
    out_dir = report_dir or config.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    filepath = os.path.join(out_dir, filename)
    tmp_path = filepath + ".building"

    usage_stats = result["usage"]
    meta_lines = [
        f"Data: CollegeFootballData — {len(data['games'])} games, league SP+/FPI/Elo "
        f"tables, betting lines and AP rankings for {label}. No web research.",
        f"Report: {result['model']} via OpenRouter — "
        f"{usage_stats.get('input_tokens') or 'N/A'} input / "
        f"{usage_stats.get('output_tokens') or 'N/A'} output tokens.",
        f"Generation time: {int(time.time() - started)}s.",
    ]

    html = render.build_html(
        home_full=f"{noun}", away_full='', year=data['season'],
        home_logo='', away_logo='',
        report_created=f"{db.format_friendly_date(today)} {today.strftime('%I:%M %p')}",
        report_markdown=result["text"], charts=chart_set, registry=registry,
        meta_lines=meta_lines,
        title=f"{label} — {noun}",
        banner=f"College Football {noun}",
        include_sources=bool(settings.get("include_sources", 1)),
        include_generation_details=bool(settings.get("include_generation_details", 1)),
    )
    try:
        render.write_pdf(html, tmp_path, footer_subject=label, footer_brand=noun)
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
                 f"({len(data['games'])} games).")
    return {"filename": filename, "seconds": elapsed,
            "season": data['season'], "week": data['week'],
            "games": len(data['games'])}


def generate_preview(**kwargs) -> dict:
    return _generate('preview', **kwargs)


def generate_wrap(**kwargs) -> dict:
    return _generate('wrap', **kwargs)
