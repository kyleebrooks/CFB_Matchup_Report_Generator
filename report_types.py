"""Registry of report types the multi-tenant API can produce.

Adding a report type means adding one entry here plus the module that builds it. The
API surface, per-account entitlements, job handling and PDF delivery all key off this
table, so nothing else needs to change.
"""

import pipeline
import team_report


class ValidationError(ValueError):
    """Bad or missing request parameters."""


def _require(params: dict, *names) -> None:
    missing = [n for n in names if not str(params.get(n) or '').strip()]
    if missing:
        raise ValidationError(f"Missing required parameter(s): {', '.join(missing)}")


def _year(params: dict):
    raw = params.get('year')
    if raw in (None, ''):
        return None
    try:
        year = int(raw)
    except (TypeError, ValueError):
        raise ValidationError("'year' must be a four-digit season, e.g. 2025")
    if not 1900 <= year <= 2200:
        raise ValidationError("'year' must be a four-digit season, e.g. 2025")
    return year


# ---------------------------------------------------------------------------
# matchup — the existing head-to-head report
# ---------------------------------------------------------------------------
def _validate_matchup(params: dict) -> dict:
    _require(params, 'home_full', 'away_full', 'home_short', 'away_short')
    return {
        'home_full': str(params['home_full']).strip(),
        'away_full': str(params['away_full']).strip(),
        'home_short': str(params['home_short']).strip(),
        'away_short': str(params['away_short']).strip(),
        'year': _year(params),
        'kickoff': (params.get('kickoff') or '').strip() or None,
    }


def _run_matchup(params: dict, progress) -> dict:
    return pipeline.generate(
        home_full=params['home_full'],
        away_full=params['away_full'],
        home_short=params['home_short'],
        away_short=params['away_short'],
        year=params.get('year'),
        kickoff=params.get('kickoff'),
        settings=params.get('settings'),
        watermark=params.get('watermark'),
        report_dir=params.get('report_dir'),
        progress=progress,
    )


# ---------------------------------------------------------------------------
# team — single-team season report
# ---------------------------------------------------------------------------
def _validate_team(params: dict) -> dict:
    _require(params, 'team_short')
    team_short = str(params['team_short']).strip()
    return {
        'team_short': team_short,
        'team_full': (params.get('team_full') or '').strip() or team_short,
        'year': _year(params),
    }


def _run_team(params: dict, progress) -> dict:
    return team_report.generate(
        team_full=params['team_full'],
        team_short=params['team_short'],
        year=params.get('year'),
        settings=params.get('settings'),
        watermark=params.get('watermark'),
        report_dir=params.get('report_dir'),
        progress=progress,
    )


REPORT_TYPES: dict[str, dict] = {
    'matchup': {
        'name': 'matchup',
        'title': 'Head-to-Head Matchup Report',
        'description': (
            'Full preview of one game: ratings, efficiency mismatches, injuries, roster and '
            'practice news, media analysis, eight charts, and a projected final score.'
        ),
        'required': ['home_full', 'away_full', 'home_short', 'away_short'],
        'optional': ['year', 'kickoff'],
        'validate': _validate_matchup,
        'run': _run_matchup,
        'subject': lambda p: f"{p['home_short']} vs {p['away_short']}",
        'dedup_key': lambda p: f"matchup:{p['home_short']}|{p['away_short']}",
    },
    'team': {
        'name': 'team',
        'title': 'Single-Team Season Report',
        'description': (
            'Season report for one program: overall outlook, game-by-game schedule '
            'breakdown, practice notes, roster news, injury report, media coverage and '
            'coach comments, with four charts.'
        ),
        'required': ['team_short'],
        'optional': ['team_full', 'year'],
        'validate': _validate_team,
        'run': _run_team,
        'subject': lambda p: p['team_short'],
        'dedup_key': lambda p: f"team:{p['team_short']}",
    },
    # Planned, not yet implemented. Listed so entitlements can be granted ahead of the
    # build and clients can discover what is coming.
    # 'conference': conference-wide report
    # 'injury':     league-wide injury sweep
}


def get(report_type: str) -> dict:
    spec = REPORT_TYPES.get((report_type or '').strip().lower())
    if not spec:
        raise ValidationError(
            f"Unknown report_type '{report_type}'. Available: {', '.join(sorted(REPORT_TYPES))}"
        )
    return spec


def catalog() -> list[dict]:
    return [
        {
            'report_type': spec['name'],
            'title': spec['title'],
            'description': spec['description'],
            'required_params': spec['required'],
            'optional_params': spec['optional'],
        }
        for spec in REPORT_TYPES.values()
    ]
