"""The two conference-scope publications.

conference_wrap — the conference's completed week: every final with the good, the
bad and the ugly, the week's top performers from the actual box scores, how every
team fared against the closing line, how the stored CFBReports projections fared
against the market and the final scores, the week's top stories from live research,
and what it all changes going forward.

conference_roundup — the week ahead: breaking news, injury reports and stories to
watch from live research, plus a board of every upcoming conference game with
kickoff time, broadcast, forecast, market lines and the model's projection — and
the stored CFBReports projection beside the market line wherever one exists in
the tracking table.

Both lean on weekly.build_week_data for the league-wide slate and rating tables,
then narrow to the conference and enrich: weather, broadcast, standings, player
stat leaders, and the predictions ledger.
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
import predictions
import render
import report as report_mod
import research
import weekly
from pipeline import PipelineError

STAGES = {
    "start":    (5,   "Starting up"),
    "gather":   (12,  "Pulling the conference's games, lines and tables"),
    "enrich":   (30,  "Adding weather, broadcasts, standings and stat leaders"),
    "ledger":   (42,  "Reading the CFBReports prediction ledger"),
    "research": (50,  "Researching news, injuries and storylines"),
    "charts":   (66,  "Rendering charts"),
    "write":    (74,  "Writing the report"),
    "pdf":      (92,  "Building the PDF"),
    "done":     (100, "Complete"),
}

WRAP_SECTIONS = [
    ("The Week in {conf}", "What actually happened across the conference this week, "
                           "in three or four paragraphs: the headline results, the "
                           "theme of the week, and where the race stands after it."),
    ("The Good, the Bad and the Ugly", "Three sub-parts. The Good: the performances "
                                       "and wins that deserve real credit. The Bad: "
                                       "the flat, costly showings. The Ugly: the "
                                       "results a fan base will want to forget. Every "
                                       "verdict anchored to scores, ratings gaps or "
                                       "postgame win probability in the data."),
    ("Top Performers", "The week's statistical standouts from the player leaders in "
                       "the data — passing, rushing, receiving and defense — with "
                       "their lines and why the performance mattered in the game."),
    ("Against the Lines", "One markdown table: every game, the closing market line, "
                          "the final margin, and who covered. Then two or three "
                          "sentences on the week's sharpest and most misleading "
                          "market reads."),
    ("The CFBReports Ledger", "How the stored CFBReports projections fared: per "
                              "predicted game, our margin vs the market's vs the "
                              "final; the running count of who was closer; and the "
                              "against-the-spread record our numbers produced. Use "
                              "ONLY the ledger in the data; if it is empty, say in "
                              "one line that no projections were on file this week."),
    ("Top Stories of the Week", "The stories that defined the conference's week, from "
                                "the researched findings — cite them. Results talk "
                                "belongs above; this is the news layer."),
    ("The Picture Moving Forward", "The updated standings table, what this week's "
                                   "results changed in the race and the postseason "
                                   "picture, and the stakes now attached to next "
                                   "week's schedule."),
]

ROUNDUP_SECTIONS = [
    ("{conf} Snapshot", "The state of the conference entering the week: the standings "
                        "table from the data, who is surging, who is slipping, and "
                        "the shape of the week ahead."),
    ("Breaking News", "The latest verified news across the conference from the "
                      "researched findings — coaching, transfers, suspensions, "
                      "program developments. Cite every item."),
    ("Injury Report", "Who is in, out, or questionable for this week's games, from "
                      "the researched findings. Cite every item; never invent a "
                      "status."),
    ("Stories to Watch", "The narratives that will decide how this week is "
                         "remembered — streaks, rivalries, hot seats, award races — "
                         "from the researched findings and the data."),
    ("The Board: Lines and Projections", "One markdown table: every upcoming game "
                                         "with kickoff (date and time), TV, "
                                         "forecast, the market line and total, the "
                                         "'Ratings consensus' column "
                                         "(model_margin_home — computed fresh for "
                                         "this report from SP+/FPI/Elo), and the "
                                         "'CFBReports projection' column — the "
                                         "margin and projected score from our "
                                         "previously published matchup report on "
                                         "that game. The two are independent "
                                         "numbers; say so in the section's opening "
                                         "line. Where cfbreports_projection has "
                                         "available=false, print exactly 'Not yet "
                                         "run' in that cell — never leave it "
                                         "blank."),
    ("Where We Differ From the Market", "The games with the widest gaps between the "
                                        "ratings consensus or CFBReports projection "
                                        "and the market line — what the "
                                        "disagreement is about, and which side of "
                                        "it the numbers support."),
    ("Game-by-Game Capsules", "For every upcoming game, a tight capsule: time, TV, "
                              "forecast, the line, the projection, and the two or "
                              "three sentences that frame it."),
    ("The Weekend's Stakes", "What each result would mean for the race, closing with "
                             "the one game that matters most."),
]

SYSTEM_PROMPT = (
    "You are a college football conference insider writing a weekly publication from "
    "verified data and researched reporting. Every number you print comes from the "
    "supplied data and every news claim from the researched findings — never invent, "
    "estimate or recall one. Plain, confident, readable prose."
)

# Research jobs are built per report from the teams actually playing: a single
# "search the conference" ask returns two or three headline items and stops,
# which is how a sixteen-team league ends up with one news story and an empty
# injury report. Naming the teams — in small batches, one call per batch —
# forces the search to visit every program's beat coverage.
_RESEARCH_BATCH = 4

_THOROUGH = ("Work through the listed teams ONE BY ONE — run a separate search "
             "for each team by name. Finding an item for one team never ends "
             "the job for the others. Report EVERY verified item; an empty "
             "report for a team with a game this week almost always means the "
             "search was not run, not that there is no news.")


def _research_jobs(kind: str, teams: list[str]) -> list[dict]:
    chunks = [teams[i:i + _RESEARCH_BATCH]
              for i in range(0, len(teams), _RESEARCH_BATCH)] or [teams]
    jobs: list[dict] = []
    if kind == 'wrap':
        for n, chunk in enumerate(chunks, 1):
            names = ', '.join(chunk)
            jobs.append({
                "key": f"conf_week_stories_{n}", "scope": "conference",
                "topic": "news", "section": "Top Stories of the Week",
                "window": 7,
                "focus": (f"the stories that defined this past week for each of "
                          f"these {{home_full}} programs: {names}. Fallout from "
                          f"the weekend's results, standout performances, "
                          f"coaching news, anything that changed a program's "
                          f"trajectory. {_THOROUGH}"),
                "exclude": "games outside this conference"})
        return jobs
    for n, chunk in enumerate(chunks, 1):
        names = ', '.join(chunk)
        jobs.append({
            "key": f"conf_news_{n}", "scope": "conference", "topic": "news",
            "section": "Breaking News", "window": 7,
            "focus": (f"breaking news for each of these {{home_full}} programs: "
                      f"{names}. Coaching changes, transfers, suspensions, "
                      f"disciplinary rulings, depth-chart shakeups and program "
                      f"developments. {_THOROUGH}"),
            "exclude": "recruiting commitments for future seasons"})
        jobs.append({
            "key": f"conf_injuries_{n}", "scope": "conference", "topic": "injury",
            "section": "Injury Report", "window": 7,
            "focus": (f"current injuries, availability designations, game-time "
                      f"decisions and return timelines for players on each of "
                      f"these {{home_full}} teams: {names}. {_THOROUGH}"),
            "exclude": "non-medical roster news"})
    jobs.append({
        "key": "conf_stories", "scope": "conference", "topic": "news",
        "section": "Stories to Watch", "window": 7,
        "focus": ("the biggest storylines around {home_full} football this week "
                  f"across these teams: {', '.join(teams)}. Winning and losing "
                  "streaks, rivalry stakes, coaches under pressure, award races, "
                  "milestone watches and the matchups with the most on the line. "
                  + _THOROUGH),
        "exclude": ""})
    return jobs


# ---------------------------------------------------------------------------
# Enrichment fetches
# ---------------------------------------------------------------------------
def _weather_by_game(api_key, year, week, season_type, errors) -> dict:
    out = {}
    for r in cfbd._get(api_key, '/games/weather',
                       {'year': year, 'week': week, 'seasonType': season_type},
                       f'Weather (week {week})', errors) or []:
        gid = r.get('id') or r.get('gameId')
        if not gid:
            continue
        keep = {k: r.get(k) for k in (
            'temperature', 'windSpeed', 'windDirection', 'precipitation',
            'humidity', 'weatherCondition', 'gameIndoors') if r.get(k) is not None}
        if keep:
            out[gid] = keep
    return out


def _media_by_game(api_key, year, week, season_type, errors) -> dict:
    out: dict[int, set] = {}
    for r in cfbd._get(api_key, '/games/media',
                       {'year': year, 'week': week, 'seasonType': season_type},
                       f'Broadcast Media (week {week})', errors) or []:
        if r.get('id') and r.get('outlet'):
            out.setdefault(r['id'], set()).add(r['outlet'])
    return {gid: sorted(v) for gid, v in out.items()}


def _standings(api_key, year, conference, sp_table, errors) -> list[dict]:
    """Conference standings with the SP+ view of each team stapled on."""
    rows = cfbd._get(api_key, '/records',
                     {'year': year, 'conference': conference},
                     f'Records ({conference})', errors) or []
    out = []
    for r in rows:
        team = r.get('team')
        total = r.get('total') or {}
        conf = r.get('conferenceGames') or {}
        sp = sp_table.get(team) or {}
        out.append({
            'team': team,
            'overall': f"{total.get('wins', 0)}-{total.get('losses', 0)}",
            'conference': f"{conf.get('wins', 0)}-{conf.get('losses', 0)}",
            'expected_wins': r.get('expectedWins'),
            'sp_rating': sp.get('rating'),
            'sp_offense': (sp.get('offense') or {}).get('rating')
                          if isinstance(sp.get('offense'), dict) else None,
            'sp_defense': (sp.get('defense') or {}).get('rating')
                          if isinstance(sp.get('defense'), dict) else None,
        })

    def _conf_pct(row):
        try:
            w, l = row['conference'].split('-')
            games = int(w) + int(l)
            return (int(w) / games) if games else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    out.sort(key=lambda r: (-_conf_pct(r), -(r['sp_rating'] or -99)))
    return out


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _player_leaders(api_key, year, week, season_type, conference, errors) -> dict:
    """The week's statistical leaders from the conference's actual box scores."""
    rows = cfbd._get(api_key, '/games/players',
                     {'year': year, 'week': week, 'seasonType': season_type,
                      'conference': conference},
                     f'Player stats ({conference} week {week})', errors) or []
    wanted = {
        ('passing', 'YDS'): 'passing_yards', ('passing', 'TD'): 'passing_tds',
        ('rushing', 'YDS'): 'rushing_yards', ('rushing', 'TD'): 'rushing_tds',
        ('receiving', 'YDS'): 'receiving_yards', ('receiving', 'TD'): 'receiving_tds',
        ('defensive', 'SACKS'): 'sacks', ('defensive', 'TOT'): 'tackles',
        ('interceptions', 'INT'): 'interceptions',
    }
    tallies: dict[str, list] = {v: [] for v in wanted.values()}
    for game in rows:
        for team in game.get('teams') or []:
            school = team.get('team') or team.get('school')
            for cat in team.get('categories') or []:
                cname = (cat.get('name') or '').lower()
                for typ in cat.get('types') or []:
                    key = (cname, (typ.get('name') or '').upper())
                    if key not in wanted:
                        continue
                    for ath in typ.get('athletes') or []:
                        val = _num(ath.get('stat'))
                        if val is not None and ath.get('name'):
                            tallies[wanted[key]].append(
                                {'player': ath['name'], 'team': school,
                                 'value': val})
    leaders = {}
    for stat, entries in tallies.items():
        entries.sort(key=lambda e: -e['value'])
        top = [e for e in entries[:6] if e['value'] > 0]
        if top:
            leaders[stat] = top
    return leaders


# ---------------------------------------------------------------------------
# The CFBReports prediction ledger
# ---------------------------------------------------------------------------
def _stored_projections(season, games) -> dict:
    """Latest stored CFBReports projection per game, keyed by game_id.

    The tracking table records one row per matchup report run; the newest row
    before now is the projection of record for that game. Rows match on game
    id when the report stored one, else on the home/away pair, compared
    case-insensitively so 'Ole Miss' and 'OLE MISS' meet.
    """
    try:
        rows = predictions.history(season=season)
    except Exception as e:
        logging.warning(f'Prediction history unavailable (non-fatal): {e}')
        return {}
    by_pair = {((g['home'] or '').lower(), (g['away'] or '').lower()): g['game_id']
               for g in games}
    out: dict[int, dict] = {}
    for row in rows:
        gid = row.get('game_id') or by_pair.get(
            ((row.get('home_short') or '').lower(),
             (row.get('away_short') or '').lower()))
        if gid is None or gid not in {g['game_id'] for g in games}:
            continue
        held = out.get(gid)
        if held and (predictions._parse_dt(row.get('created_at')) or datetime.min) <= \
                (predictions._parse_dt(held.get('_created')) or datetime.min):
            continue
        out[gid] = {
            'available': True,
            'margin_home': row.get('consensus_margin'),
            'projected_home': row.get('projected_home'),
            'projected_away': row.get('projected_away'),
            'projected_total': row.get('projected_total'),
            'home_win_probability': row.get('home_win_probability'),
            'market_margin_when_projected': row.get('market_margin'),
            'run_date': row.get('run_date'),
            '_created': row.get('created_at'),
        }
    for v in out.values():
        v.pop('_created', None)
    return out


# A game the tracking table has never seen gets an explicit marker, so the
# board prints "Not yet run" instead of a blank the reader cannot interpret.
_NO_PROJECTION = {'available': False,
                  'note': 'No CFBReports matchup report has been run for this '
                          'game yet.'}


def _ats_result(final_margin, market_margin) -> str | None:
    if final_margin is None or market_margin is None:
        return None
    diff = final_margin - market_margin
    if abs(diff) < 0.01:
        return 'push'
    return 'home covered' if diff > 0 else 'away covered'


def _ledger(games) -> dict:
    """The wrap's verdict on the stored projections: us vs the market vs reality."""
    rows, model_closer, market_closer, ats_w, ats_l, ats_p = [], 0, 0, 0, 0, 0
    for g in games:
        proj = g.get('cfbreports_projection') or {}
        ours = proj.get('margin_home')
        market = g.get('market_margin_home')
        actual = g.get('margin')
        if ours is None or actual is None:
            continue
        our_err = round(abs(float(ours) - actual), 1)
        market_err = (round(abs(float(market) - actual), 1)
                      if market is not None else None)
        closer = None
        if market_err is not None:
            closer = ('CFBReports' if our_err < market_err
                      else 'market' if market_err < our_err else 'tie')
            if closer == 'CFBReports':
                model_closer += 1
            elif closer == 'market':
                market_closer += 1
        pick = None
        if market is not None:
            gap = float(ours) - float(market)
            if abs(gap) >= 0.01:
                picked_home = gap > 0
                result = _ats_result(actual, float(market))
                if result == 'push':
                    pick, ats_p = 'push', ats_p + 1
                else:
                    won = (result == 'home covered') == picked_home
                    pick = 'won' if won else 'lost'
                    ats_w, ats_l = ats_w + won, ats_l + (not won)
        rows.append({
            'home': g['home'], 'away': g['away'],
            'cfbreports_margin': ours, 'market_margin': market,
            'final_margin': actual, 'cfbreports_error': our_err,
            'market_error': market_err, 'closer_to_reality': closer,
            'against_the_spread': pick,
        })
    return {
        'games_with_projections': len(rows),
        'cfbreports_closer': model_closer,
        'market_closer': market_closer,
        'ats_record': f'{ats_w}-{ats_l}-{ats_p}',
        'per_game': rows,
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_conference_data(api_key, conference, year=None, week=None, *,
                          completed: bool) -> dict:
    base = weekly.build_week_data(api_key, year, week, completed=completed)
    errors = base['errors']
    conf_l = (conference or '').strip().lower()
    games = [g for g in base['games']
             if conf_l in ((g.get('home_conference') or '').lower(),
                           (g.get('away_conference') or '').lower())]
    # The canonical conference name as CFBD spells it, for headings and fetches.
    canonical = conference.strip()
    for g in games:
        for field in ('home_conference', 'away_conference'):
            if (g.get(field) or '').lower() == conf_l:
                canonical = g[field]
                break
        else:
            continue
        break

    y, w, st = base['season'], base['week'], base['season_type']
    known = sorted({c for g in base['games']
                    for c in (g.get('home_conference'), g.get('away_conference'))
                    if c})
    if not games:
        # Nothing to enrich; the caller raises with the known-conference hint.
        return {'season': y, 'week': w, 'season_type': st,
                'conference': canonical, 'games': [], 'standings': [],
                'player_leaders': {}, 'ledger': None, 'top25': base['top25'],
                'teams_this_week': [], 'team_logos': {},
                'known_conferences': known, 'errors': errors,
                'auth_failures': base['auth_failures']}

    weather = _weather_by_game(api_key, y, w, st, errors)
    media = _media_by_game(api_key, y, w, st, errors)
    projections = _stored_projections(y, games)
    for g in games:
        g['tv'] = media.get(g['game_id']) or g.get('tv')
        g['forecast'] = weather.get(g['game_id'])
        g['cfbreports_projection'] = projections.get(g['game_id'], _NO_PROJECTION)
        if completed:
            g['against_the_spread'] = _ats_result(g.get('margin'),
                                                  g.get('market_margin_home'))

    sp_table = {}
    try:
        sp_table = weekly._table(
            cfbd._get(api_key, '/ratings/sp', {'year': y}, 'SP', errors), 'team')
    except Exception:
        pass
    standings = _standings(api_key, y, canonical, sp_table, errors)
    leaders = (_player_leaders(api_key, y, w, st, canonical, errors)
               if completed else {})

    # The teams in this week's games, home teams first — this drives the
    # research batches — plus each team's logo for the standings strip.
    teams_this_week = sorted({t for g in games for t in (g['home'], g['away'])
                              if t})
    logos: dict[str, str] = {}
    try:
        wanted = {s['team'] for s in standings} | set(teams_this_week)
        for row in cfbd.all_teams(api_key, y, errors) or []:
            school = row.get('school')
            if school in wanted and (row.get('logos') or []):
                logos[school] = row['logos'][0]
    except Exception as e:
        logging.warning(f'Team logos unavailable (non-fatal): {e}')

    return {
        'season': y, 'week': w, 'season_type': st,
        'conference': canonical, 'games': games,
        'standings': standings, 'player_leaders': leaders,
        'ledger': _ledger(games) if completed else None,
        'top25': base['top25'],
        'teams_this_week': teams_this_week,
        'team_logos': logos,
        'known_conferences': known,
        'errors': errors,
        'auth_failures': base['auth_failures'],
    }


def _generate(kind: str, *, conference: str, year=None, week=None, settings=None,
              watermark=None, report_dir=None, progress=None) -> dict:
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
    missing = [n for n, v in (("CFBD", cfbd_api_key),
                              ("OpenRouter", openrouter_api_key)) if not v]
    if missing:
        raise PipelineError(f"Missing required API key(s): {', '.join(missing)}")

    step("gather")
    completed = kind == 'wrap'
    data = build_conference_data(cfbd_api_key, conference, year, week,
                                 completed=completed)
    if data['auth_failures'] and len(data['auth_failures']) >= 3:
        first = data['auth_failures'][0]
        raise PipelineError("CollegeFootballData rejected the API key",
                            f"HTTP {first['status']} — {first['body']}", 502)
    if not data['games']:
        which = 'finals' if completed else 'scheduled games'
        hint = (f"Known conferences this week: {', '.join(data['known_conferences'])}."
                if data['known_conferences'] else
                "Pick a different week, or wait for the schedule to post.")
        raise PipelineError(
            f"No {conference} {which} found for {data['season']} "
            f"week {data['week']}", hint, 404)

    step("enrich")
    conf = data['conference']
    step("ledger")

    step("research")
    registry = research.SourceRegistry()
    registry.add("https://collegefootballdata.com", "College Football Data API",
                 "CollegeFootballData")
    ctx = {
        'home_full': conf, 'away_full': '', 'home_short': conf, 'away_short': '',
        'year': data['season'], 'kickoff': None,
        'now_utc': datetime.now(timezone.utc),
    }
    jobs = _research_jobs('wrap' if completed else 'roundup',
                          data['teams_this_week'])
    findings: dict[str, list] = {}
    try:
        raw = research.run_research(openrouter_api_key, ctx, settings, jobs=jobs)
    except Exception as e:
        logging.warning(f"Conference research failed (continuing data-only): {e}")
        raw = {}
    for job in jobs:
        bucket = raw.get(job['key']) or {}
        items = findings.setdefault(job['section'], [])
        for f in bucket.get('findings', []):
            idx = registry.add(f.get('source_url', ''), f.get('headline', ''),
                               f.get('source_name', ''))
            item = {k: f.get(k) for k in ('headline', 'detail', 'team', 'player',
                                          'position', 'status', 'impact',
                                          'source_name', 'published')
                    if f.get(k)}
            item['citation'] = f"[{idx}]" if idx else ""
            items.append(item)
        # Pages the search engine surfaced beyond what the model attributed:
        # registered so the SOURCES list reflects everything that was read.
        for cit in bucket.get('citations') or []:
            registry.add(cit.get('url', ''), cit.get('title', ''), '')

    step("charts")
    chart_kind = 'wrap' if completed else 'roundup'
    extras = {}
    if completed:
        errs: list[dict] = []
        conf_ids = {g['game_id'] for g in data['games']}
        finals = weekly._raw_completed(cfbd_api_key, data['season'], data['week'],
                                       data['season_type'], errs)
        extras['finals_detail'] = [f for f in finals if f['game_id'] in conf_ids]
    try:
        chart_set = charts_mod.build_conference_charts(chart_kind, data, extras)
    except charts_mod.ChartsUnavailable as e:
        raise PipelineError("Charting library missing on the server", str(e), 500)

    sections = WRAP_SECTIONS if completed else ROUNDUP_SECTIONS
    section_text = "\n".join(
        f"{i}. {t.format(conf=conf)} — {g}" for i, (t, g) in enumerate(sections, 1))
    charts_text = "\n".join(f'- "{c["title"]}" (rendered): {c["caption"]}'
                            for c in chart_set)

    import json as _json
    bundle = {
        'conference': conf,
        'week': {k: data[k] for k in ('season', 'week', 'season_type')},
        'standings': data['standings'],
        'games': [{k: v for k, v in g.items() if not k.startswith('_')}
                  for g in data['games']],
        'player_leaders': data['player_leaders'],
        'cfbreports_ledger': data['ledger'],
        'ap_top25': data['top25'],
        'researched_findings': findings,
    }
    label = f"{conf} — {data['season']} Week {data['week']}"
    noun = 'Conference Weekly Wrap' if completed else 'Conference Roundup'
    prompt = f"""Write the complete {noun} for the {conf} covering {data['season']} \
Week {data['week']} of the college football season.

Produce EXACTLY these sections, in this order, and nothing else. No preamble, no
sign-off.

{section_text}

FORMAT RULES:
- Level-2 markdown headings ("## Section Title"), one short opening line per section.
- Markdown tables where the section calls for them; keep rows terse; always a blank
  line before a table. Never repeat a section.
- EVERY number comes from the DATA below — never invent, estimate or recall one.
- News, injuries and stories come ONLY from researched_findings, cited with each
  item's marker. If a findings list is empty, say so in one honest line.
- News, injury and story items are formatted so a reader can scan by school:
  every item starts on its own line as "**Team** — headline: detail [n]". Group
  several items for the same team under one bold team lead-in. The Injury
  Report uses a markdown table with columns Team | Player (Pos) | Status |
  Impact wherever the findings carry player detail.
- Kickoff times: each game's start is UTC — present times with the date and note
  the tv networks from the game's tv field where present.

RENDERED CHARTS (already placed in the report — reference, do not re-describe):
{charts_text}

PRE-ASSIGNED CITATION MARKERS:
[1] CollegeFootballData (games, standings, rating tables, betting lines, weather,
broadcast and player statistics).

DATA:
{_json.dumps(bundle, separators=(',', ':'), default=str)}
"""

    step("write")
    ctx2 = {'home_team': label, 'away_team': '', 'year': data['season']}
    try:
        result = report_mod.generate(
            openrouter_api_key, ctx2, bundle, chart_set, registry, settings,
            prompt=prompt, system_prompt=SYSTEM_PROMPT)
    except Exception as e:
        logging.exception("Conference report generation failed")
        raise PipelineError("Report model request failed", str(e)[:500], 502)

    step("pdf")
    today = datetime.now()
    prefix = 'confwrap' if completed else 'confround'
    filename = (f"{prefix}_{conf} {data['season']} Week {data['week']}_"
                f"{db.format_friendly_date(today)}.pdf")
    out_dir = report_dir or config.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    filepath = os.path.join(out_dir, filename)
    tmp_path = filepath + ".building"

    usage_stats = result["usage"]
    n_proj = sum(1 for g in data['games'] if g.get('cfbreports_projection'))
    meta_lines = [
        f"Data: CollegeFootballData — {len(data['games'])} {conf} games, standings, "
        f"rating tables, betting lines, weather and broadcast for {label}; "
        f"{n_proj} stored CFBReports projection(s) from the tracking table.",
        f"Research: live web research for news, injuries and storylines.",
        f"Report: {result['model']} via OpenRouter — "
        f"{usage_stats.get('input_tokens') or 'N/A'} input / "
        f"{usage_stats.get('output_tokens') or 'N/A'} output tokens.",
        f"Generation time: {int(time.time() - started)}s.",
    ]

    html = render.build_html(
        home_full=noun, away_full='', year=data['season'],
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
                 f"({len(data['games'])} games, {n_proj} projections).")
    return {"filename": filename, "seconds": elapsed,
            "season": data['season'], "week": data['week'],
            "conference": conf, "games": len(data['games'])}


def generate_wrap(**kwargs) -> dict:
    return _generate('wrap', **kwargs)


def generate_roundup(**kwargs) -> dict:
    return _generate('roundup', **kwargs)
