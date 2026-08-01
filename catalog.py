"""What each report contains and how each section is produced.

Assembled from the live definitions — report_types, the research job lists, the section
plans and the chart specs — so it cannot drift from what the service actually does. If a
section is added to a report, it appears here without anyone remembering to update a doc.
"""

import charts
import config
import report as report_mod
import report_types
import research
import settings_store
import team_report

# How each section gets its content.
LIVE_WEB = 'live web research (one dedicated search call)'
LIVE_WEB_PLUS_FEED = 'live web research + the Rotowire feed (two labelled buckets)'
CFBD = 'CollegeFootballData statistics only — no LLM research'
SYNTHESIS = 'synthesised by the report model from everything else; no search of its own'
COMPUTED = 'computed in Python, then handed to the report model as an anchor'


def _matchup_sections() -> list[dict]:
    """Map the matchup section plan onto its source of truth."""
    home, away = '{home}', '{away}'
    plan = report_mod._section_plan(home, away)
    research_titles = {
        f'{home} Injury Updates': ('home_injuries', LIVE_WEB_PLUS_FEED),
        f'{away} Injury Updates': ('away_injuries', LIVE_WEB_PLUS_FEED),
        'Key Player Matchups': ('key_player_matchups', LIVE_WEB),
        f'{home} Roster Updates': ('home_roster', LIVE_WEB),
        f'{away} Roster Updates': ('away_roster', LIVE_WEB),
        f'{home} Practice and Scrimmage Updates': ('home_practice', LIVE_WEB),
        f'{away} Practice and Scrimmage Updates': ('away_practice', LIVE_WEB),
        f'{home} vs {away} Media Matchup Analysis': ('media_analysis', LIVE_WEB),
    }
    windows = {job['key']: job['window'] for job in research.RESEARCH_JOBS}

    out = []
    for title, guidance in plan:
        job_key, source = research_titles.get(title, (None, None))
        if source is None:
            source = SYNTHESIS if title in ('Matchup Overview',) else CFBD
            if title == 'Final Prediction':
                source = f'{SYNTHESIS}, anchored to the statistical baseline ({COMPUTED})'
        out.append({
            'title': title,
            'source': source,
            'research_key': job_key,
            'window_days': windows.get(job_key),
            'guidance': guidance,
        })
    return out


def _team_sections() -> list[dict]:
    windows = {
        'schedule': 30,
        'practice': config.PRACTICE_WINDOW_DAYS,
        'roster': config.ROSTER_WINDOW_DAYS,
        'injuries': config.INJURY_WINDOW_DAYS,
        'media': config.MEDIA_WINDOW_DAYS,
        'coaches': config.MEDIA_WINDOW_DAYS,
    }
    out = []
    for title, job_key, guidance in team_report.SECTIONS:
        if job_key is None:
            source = SYNTHESIS
        elif job_key == 'injuries':
            source = LIVE_WEB_PLUS_FEED
        else:
            source = LIVE_WEB
        out.append({
            'title': title,
            'source': source,
            'research_key': job_key,
            'window_days': windows.get(job_key),
            'guidance': guidance,
        })
    return out


def describe(report_type: str) -> dict:
    """Everything about one report type: sections, charts, models, data sources."""
    spec = report_types.get(report_type)
    settings = settings_store.resolved_defaults()

    if report_type == 'matchup':
        sections = _matchup_sections()
        chart_specs = [(k, t, c) for k, t, c in charts.CHART_SPECS]
        research_count = len(research.RESEARCH_JOBS)
        data_sources = [
            'CollegeFootballData — ratings, advanced stats, PPA, talent, returning '
            'production, schedule, betting lines (25 requests)',
            'Rotowire injury feed — local SQLite, filtered to the two teams',
            f'Live web research — {research_count} parallel calls, one per news section',
            'Computed baseline — SP+/FPI/Elo blended with the market line',
        ]
    elif report_type == 'team':
        sections = _team_sections()
        chart_specs = [(k, t, c) for k, t, c in charts.TEAM_CHART_SPECS]
        research_count = 6
        data_sources = [
            'CollegeFootballData — ratings, advanced stats, PPA, talent, full schedule, '
            'records (15 requests)',
            'Rotowire injury feed — local SQLite, filtered to this team',
            f'Live web research — {research_count} parallel calls, one per news section',
        ]
    else:
        sections, chart_specs, research_count, data_sources = [], [], 0, []

    return {
        'report_type': spec['name'],
        'title': spec['title'],
        'description': spec['description'],
        'required_params': spec['required'],
        'optional_params': spec['optional'],
        'sections': sections,
        'charts': [{'key': k, 'title': t, 'caption': c} for k, t, c in chart_specs],
        'research_calls': research_count,
        'data_sources': data_sources,
        'research_model': settings['research_model'],
        'report_model': settings['report_model'],
        'search_engine': settings['search_engine'] or 'auto',
        'search_max_results': settings['search_max_results'],
    }


def all_reports() -> list[dict]:
    return [describe(name) for name in sorted(report_types.REPORT_TYPES)]


PIPELINE_NOTES = [
    'Every report follows the same three stages:',
    '',
    '  1. GATHER (parallel)  CFBD statistics and the live web research calls run at the',
    '                        same time. Each news section gets its own dedicated search',
    '                        with its own recency window, so one topic cannot crowd out',
    '                        another. Statistics are never researched by an LLM.',
    '',
    '  2. ASSEMBLE (Python)  Findings are merged into per-section buckets. A section fed',
    '                        by more than one input keeps them separate and separately',
    '                        cited. Every retrieved URL is registered once and assigned a',
    '                        stable [n] marker. Charts are rendered from the CFBD data.',
    '',
    '  3. WRITE (one call)   The report model receives the statistics, the buckets, the',
    '                        pre-assigned citation markers and the chart manifest, and',
    '                        writes the prose. It has NO web access — it can only use',
    '                        what stage 1 gathered.',
    '',
    'The SOURCES list in the PDF is rendered from the citation registry, not by the',
    'model, so a citation can never point at a URL that was invented or dropped.',
    'Every page is stamped with the account watermark, or the service default.',
]
