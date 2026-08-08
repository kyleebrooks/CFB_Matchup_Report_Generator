"""Registry of report types the multi-tenant API can produce.

Adding a report type means adding one entry here plus the module that builds it. The
API surface, per-account entitlements, job handling and PDF delivery all key off this
table, so nothing else needs to change.
"""

import game_recap
import pipeline
import prediction_reports
import season_plays
import team_report
import weekly


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


def _week(params: dict):
    raw = params.get('week')
    if raw in (None, ''):
        return None
    try:
        week = int(raw)
    except (TypeError, ValueError):
        raise ValidationError("'week' must be a week number, e.g. 9")
    if not 0 <= week <= 30:
        raise ValidationError("'week' must be a week number, e.g. 9")
    return week


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
        account_id=params.get('account_id'),
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


# ---------------------------------------------------------------------------
# season_plays — every play of one team's season, analysed in depth
# ---------------------------------------------------------------------------
def _run_season_plays(params: dict, progress) -> dict:
    return season_plays.generate(
        team_full=params['team_full'],
        team_short=params['team_short'],
        year=params.get('year'),
        settings=params.get('settings'),
        watermark=params.get('watermark'),
        report_dir=params.get('report_dir'),
        progress=progress,
    )


# ---------------------------------------------------------------------------
# full_game_recap — a finished game, from the game record alone
# ---------------------------------------------------------------------------
def _validate_recap(params: dict) -> dict:
    _require(params, 'game_id')
    try:
        game_id = int(str(params['game_id']).strip())
    except (TypeError, ValueError):
        raise ValidationError("'game_id' must be a CollegeFootballData game id "
                              "(an integer — GET /v1/games lists them)")
    if game_id <= 0:
        raise ValidationError("'game_id' must be a positive integer")
    return {'game_id': game_id}


def _run_recap(params: dict, progress) -> dict:
    return game_recap.generate(
        game_id=params['game_id'],
        settings=params.get('settings'),
        watermark=params.get('watermark'),
        report_dir=params.get('report_dir'),
        progress=progress,
    )


# ---------------------------------------------------------------------------
# weekly_preview / weekly_wrap — the whole slate, before and after
# ---------------------------------------------------------------------------
def _validate_weekly(params: dict) -> dict:
    return {'year': _year(params), 'week': _week(params)}


def _run_weekly(generator):
    def run(params: dict, progress) -> dict:
        return generator(
            year=params.get('year'),
            week=params.get('week'),
            settings=params.get('settings'),
            watermark=params.get('watermark'),
            report_dir=params.get('report_dir'),
            progress=progress,
        )
    return run


def _weekly_subject(p: dict) -> str:
    if p.get('year') and p.get('week'):
        return f"{p['year']} week {p['week']}"
    return "current week"


# ---------------------------------------------------------------------------
# prediction_audit / prediction_review — the prediction record, graded
# ---------------------------------------------------------------------------
def _validate_predictions(params: dict) -> dict:
    return {'year': _year(params)}


def _run_predictions(kind):
    def run(params: dict, progress) -> dict:
        return prediction_reports.generate(
            kind,
            year=params.get('year'),
            settings=params.get('settings'),
            watermark=params.get('watermark'),
            report_dir=params.get('report_dir'),
            progress=progress,
        )
    return run


def _prediction_subject(p: dict) -> str:
    return f"predictions {p['year']}" if p.get('year') else "predictions"


REPORT_TYPES: dict[str, dict] = {
    'matchup': {
        'name': 'matchup',
        'title': 'Head-to-Head Matchup Report',
        'description': (
            'Full preview of one game: ratings, efficiency mismatches, injuries, verified '
            'transfer-portal moves, the real head-to-head series, game-day weather and '
            'broadcast, against-the-spread records, practice news, media analysis, nine '
            'charts, and a scoreboard-style final prediction card.'
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
            'coach comments, with five charts including the remaining-schedule outlook.'
        ),
        'required': ['team_short'],
        'optional': ['team_full', 'year'],
        'validate': _validate_team,
        'run': _run_team,
        'subject': lambda p: p['team_short'],
        'dedup_key': lambda p: f"team:{p['team_short']}",
    },
    'season_plays': {
        'name': 'season_plays',
        'title': 'Full Season Play-by-Play Analysis',
        'description': (
            'Every play from every completed game one team played in a season, '
            'analysed in depth: play-group profile, where the runs went by gap and '
            'side, pass depth and pressure, down-and-distance tendencies, third and '
            'fourth down, red zone, a computed season play-calling grade, the same '
            'cuts for what the defense allowed, week-to-week evolution, and a '
            'closing scouting report — with four charts from the season '
            'play-by-play.'
        ),
        'required': ['team_short'],
        'optional': ['team_full', 'year'],
        'validate': _validate_team,
        'run': _run_season_plays,
        'subject': lambda p: p['team_short'],
        'dedup_key': lambda p: f"season_plays:{p['team_short']}|{p.get('year')}",
    },
    'full_game_recap': {
        'name': 'full_game_recap',
        'title': 'Full Game Recap',
        'description': (
            'Post-game breakdown of one finished game, built entirely from the game '
            'record: the pregame expectations it was measured against, how it '
            'unfolded, the drives that decided it, what went right and wrong, '
            'computed play-calling grades for both staffs, player and unit grades at '
            'both ends of the scale, and what could have been done differently — '
            'with six charts from the play-by-play, including the win-probability '
            'story of the game.'
        ),
        'required': ['game_id'],
        'optional': [],
        'validate': _validate_recap,
        'run': _run_recap,
        'subject': lambda p: f"game {p['game_id']}",
        'dedup_key': lambda p: f"recap:{p['game_id']}",
    },
    'weekly_preview': {
        'name': 'weekly_preview',
        'title': 'Weekly Slate Preview',
        'description': (
            'The full upcoming week in one report: games of the week, a slate board '
            'with model and market lines for every game, upset watch, ranked teams on '
            'alert, and a day-by-day viewing guide — with charts comparing model '
            'projections to the betting market.'
        ),
        'required': [],
        'optional': ['year', 'week'],
        'validate': _validate_weekly,
        'run': _run_weekly(weekly.generate_preview),
        'subject': _weekly_subject,
        'dedup_key': lambda p: f"weekly_preview:{p.get('year')}|{p.get('week')}",
    },
    'weekly_wrap': {
        'name': 'weekly_wrap',
        'title': 'Weekly Wrap',
        'description': (
            'The completed week in review: biggest surprises against the ratings, '
            'rating movers, closest escapes and worst blowouts, the best games by '
            'excitement, an overreaction check, and what the results set up next.'
        ),
        'required': [],
        'optional': ['year', 'week'],
        'validate': _validate_weekly,
        'run': _run_weekly(weekly.generate_wrap),
        'subject': _weekly_subject,
        'dedup_key': lambda p: f"weekly_wrap:{p.get('year')}|{p.get('week')}",
    },
    'prediction_audit': {
        'name': 'prediction_audit',
        'title': 'Prediction Audit (internal)',
        'description': (
            'Internal accuracy audit of every stored matchup prediction: grades open '
            'predictions against final scores, compares report models head to head, '
            'and shows how accuracy moves as game day approaches. Names models — '
            'for operators, not the public.'
        ),
        'required': [],
        'optional': ['year'],
        'validate': _validate_predictions,
        'run': _run_predictions('audit'),
        'subject': _prediction_subject,
        'dedup_key': lambda p: f"prediction_audit:{p.get('year')}",
    },
    'prediction_review': {
        'name': 'prediction_review',
        'title': 'Prediction Review',
        'description': (
            'Public review of the prediction record: overall accuracy, how projections '
            'sharpen as kickoff approaches, signature calls and honest misses. No '
            'models or internal mechanics — prediction analysis only.'
        ),
        'required': [],
        'optional': ['year'],
        'validate': _validate_predictions,
        'run': _run_predictions('review'),
        'subject': _prediction_subject,
        'dedup_key': lambda p: f"prediction_review:{p.get('year')}",
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
