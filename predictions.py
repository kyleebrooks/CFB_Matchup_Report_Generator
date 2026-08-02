"""Every matchup report's prediction, kept forever and graded against reality.

Each successful matchup report files one row here: when it ran, the game it was about,
the full statistical baseline it predicted (margin, total, projected score, win
probability, the market line at that moment), and WHICH REPORT MODEL wrote it. Once
the game finishes, the row is graded: actual score, margin error, winner right or
wrong, against-the-spread and total results.

Two report types read this table — an internal audit that compares models and shows
how predictions move as game day approaches, and a public review that shows the
prediction analysis with the machinery redacted.

Recording is fail-soft: a report must never fail because bookkeeping did.
"""

import json
import logging
from datetime import date, datetime

import db

TABLE = 'matchup_predictions'

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    account_id INT NULL,
    created_at DATETIME NOT NULL,
    run_date CHAR(10) NOT NULL,
    season INT NULL,
    week INT NULL,
    game_id BIGINT NULL,
    game_date DATETIME NULL,
    home_short VARCHAR(80) NOT NULL,
    away_short VARCHAR(80) NOT NULL,
    home_full VARCHAR(120) NULL,
    away_full VARCHAR(120) NULL,
    report_model VARCHAR(120) NULL,
    report_filename VARCHAR(255) NULL,
    consensus_margin DOUBLE NULL,
    ratings_margin DOUBLE NULL,
    market_margin DOUBLE NULL,
    market_total DOUBLE NULL,
    projected_total DOUBLE NULL,
    projected_home DOUBLE NULL,
    projected_away DOUBLE NULL,
    home_win_probability DOUBLE NULL,
    baseline_json MEDIUMTEXT NULL,
    graded TINYINT(1) NOT NULL DEFAULT 0,
    actual_home INT NULL,
    actual_away INT NULL,
    actual_margin INT NULL,
    margin_error DOUBLE NULL,
    winner_correct TINYINT(1) NULL,
    ats_result VARCHAR(10) NULL,
    total_result VARCHAR(10) NULL,
    graded_at DATETIME NULL,
    KEY idx_pred_game (season, home_short, away_short),
    KEY idx_pred_graded (graded, game_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

COLUMNS = [
    'id', 'account_id', 'created_at', 'run_date', 'season', 'week', 'game_id',
    'game_date', 'home_short', 'away_short', 'home_full', 'away_full',
    'report_model', 'report_filename', 'consensus_margin', 'ratings_margin',
    'market_margin', 'market_total', 'projected_total', 'projected_home',
    'projected_away', 'home_win_probability', 'baseline_json', 'graded',
    'actual_home', 'actual_away', 'actual_margin', 'margin_error',
    'winner_correct', 'ats_result', 'total_result', 'graded_at',
]


def ensure_schema() -> None:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
    finally:
        conn.close()


def record(*, account_id, baseline: dict, report_model: str, report_filename: str,
           home_full: str, away_full: str, season, week=None, game_id=None,
           game_date=None, now: datetime | None = None) -> None:
    """File one prediction row. Never raises — reports outrank bookkeeping."""
    try:
        now = now or datetime.utcnow()
        projection = (baseline or {}).get('projected_score') or {}
        ensure_schema()
        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {TABLE} (account_id, created_at, run_date, season, "
                    f"week, game_id, game_date, home_short, away_short, home_full, "
                    f"away_full, report_model, report_filename, consensus_margin, "
                    f"ratings_margin, market_margin, market_total, projected_total, "
                    f"projected_home, projected_away, home_win_probability, "
                    f"baseline_json, graded) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    f"%s,%s,%s,%s,%s,0)",
                    (account_id, now, now.strftime('%Y-%m-%d'), season, week,
                     game_id, game_date,
                     baseline.get('home_team'), baseline.get('away_team'),
                     home_full, away_full, report_model, report_filename,
                     baseline.get('consensus_margin'),
                     baseline.get('ratings_consensus_margin'),
                     baseline.get('market_margin'), baseline.get('market_total'),
                     baseline.get('projected_total'),
                     projection.get('home_score'), projection.get('away_score'),
                     baseline.get('home_win_probability'),
                     json.dumps(baseline, default=str)),
                )
        finally:
            conn.close()
        logging.info(f"Prediction recorded: {baseline.get('home_team')} vs "
                     f"{baseline.get('away_team')} margin "
                     f"{baseline.get('consensus_margin')} ({report_model})")
    except Exception as e:
        logging.warning(f"Prediction record failed (non-fatal): {e}")


def _rows(where: str = '', params: tuple = ()) -> list[dict]:
    conn = db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} {where}", params)
            return [dict(zip(COLUMNS, r)) for r in cur.fetchall() or []]
    finally:
        conn.close()


def _parse_dt(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        return None


def _game_key(row: dict) -> tuple:
    if row.get('game_id'):
        return ('g', row['game_id'])
    return ('t', row.get('season'), row.get('home_short'), row.get('away_short'))


def latest_per_day(rows: list[dict]) -> list[dict]:
    """One prediction per game per run day — the most recent of that day wins."""
    best: dict = {}
    for row in rows:
        key = (_game_key(row), row.get('run_date'))
        held = best.get(key)
        if held is None or _parse_dt(row['created_at']) > _parse_dt(held['created_at']):
            best[key] = row
    return sorted(best.values(),
                  key=lambda r: (_parse_dt(r['created_at']) or datetime.min))


def history(season=None, graded_only=False, limit=2000) -> list[dict]:
    where, params = [], []
    if season:
        where.append('season = %s')
        params.append(season)
    if graded_only:
        where.append('graded = 1')
    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    rows = _rows(f"{clause} ORDER BY created_at DESC LIMIT {int(limit)}", tuple(params))
    return latest_per_day(rows)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
def _grade_row(row: dict, game: dict) -> dict | None:
    """The grades for one finished game, or None if it cannot be graded."""
    home_pts, away_pts = game.get('homePoints'), game.get('awayPoints')
    if home_pts is None or away_pts is None:
        return None
    actual_margin = int(home_pts) - int(away_pts)
    grades = {'actual_home': int(home_pts), 'actual_away': int(away_pts),
              'actual_margin': actual_margin}

    consensus = row.get('consensus_margin')
    if consensus is not None:
        grades['margin_error'] = round(abs(float(consensus) - actual_margin), 2)
        picked_home = float(consensus) > 0
        won_home = actual_margin > 0
        grades['winner_correct'] = 1 if (actual_margin != 0
                                         and picked_home == won_home) else 0

    market = row.get('market_margin')
    if consensus is not None and market is not None:
        # The model's side of the spread: home when it projects MORE than the line.
        if float(consensus) == float(market) or actual_margin == float(market):
            grades['ats_result'] = 'push'
        else:
            picked_home_ats = float(consensus) > float(market)
            home_covered = actual_margin > float(market)
            grades['ats_result'] = 'win' if picked_home_ats == home_covered else 'loss'
    else:
        grades['ats_result'] = 'na'

    projected_total = row.get('projected_total')
    market_total = row.get('market_total')
    actual_total = int(home_pts) + int(away_pts)
    if projected_total is not None and market_total is not None:
        if float(projected_total) == float(market_total) or actual_total == float(market_total):
            grades['total_result'] = 'push'
        else:
            picked_over = float(projected_total) > float(market_total)
            went_over = actual_total > float(market_total)
            grades['total_result'] = 'win' if picked_over == went_over else 'loss'
    else:
        grades['total_result'] = 'na'
    return grades


def grade_pending(cfbd_api_key: str, limit: int = 200) -> dict:
    """Grade every ungraded prediction whose game has finished. Returns counts."""
    import cfbd

    rows = _rows('WHERE graded = 0 ORDER BY created_at ASC LIMIT %s', (int(limit),))
    if not rows:
        return {'checked': 0, 'graded': 0}

    graded = 0
    game_cache: dict = {}
    for row in rows:
        game = None
        if row.get('game_id'):
            key = ('id', row['game_id'])
            if key not in game_cache:
                game_cache[key] = cfbd._first(
                    cfbd._get(cfbd_api_key, '/games', {'id': row['game_id']},
                              f"Grade game {row['game_id']}"))
            game = game_cache[key]
        elif row.get('season'):
            key = ('team', row['season'], row['home_short'])
            if key not in game_cache:
                game_cache[key] = cfbd._get(
                    cfbd_api_key, '/games',
                    {'year': row['season'], 'team': row['home_short']},
                    f"Grade games {row['home_short']}") or []
            game = next((g for g in game_cache[key]
                         if g.get('awayTeam') == row.get('away_short')
                         and g.get('completed')), None)

        if not game or not game.get('completed'):
            continue
        grades = _grade_row(row, game)
        if not grades:
            continue

        conn = db.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {TABLE} SET graded=1, actual_home=%s, actual_away=%s, "
                    f"actual_margin=%s, margin_error=%s, winner_correct=%s, "
                    f"ats_result=%s, total_result=%s, graded_at=%s, game_id=%s, "
                    f"game_date=%s WHERE id=%s",
                    (grades['actual_home'], grades['actual_away'],
                     grades['actual_margin'], grades.get('margin_error'),
                     grades.get('winner_correct'), grades.get('ats_result'),
                     grades.get('total_result'), datetime.utcnow(),
                     game.get('id') or row.get('game_id'),
                     _parse_dt(game.get('startDate')), row['id']),
                )
        finally:
            conn.close()
        graded += 1

    if graded:
        logging.info(f"Predictions graded: {graded} of {len(rows)} pending")
    return {'checked': len(rows), 'graded': graded}


# ---------------------------------------------------------------------------
# Aggregations for the audit and review reports (computed in Python: the volumes
# are small and it keeps the SQL portable across MySQL and the test stand-in)
# ---------------------------------------------------------------------------
def _pct(part: int, whole: int):
    return round(part / whole * 100, 1) if whole else None


def _summarise(rows: list[dict]) -> dict:
    graded = [r for r in rows if r.get('graded')]
    winners = [r for r in graded if r.get('winner_correct') is not None]
    ats = [r for r in graded if r.get('ats_result') in ('win', 'loss')]
    totals = [r for r in graded if r.get('total_result') in ('win', 'loss')]
    errors = [float(r['margin_error']) for r in graded
              if r.get('margin_error') is not None]
    return {
        'predictions': len(rows),
        'graded': len(graded),
        'mean_abs_error': round(sum(errors) / len(errors), 2) if errors else None,
        'winner_pct': _pct(sum(r['winner_correct'] for r in winners), len(winners)),
        'ats_record': f"{sum(1 for r in ats if r['ats_result'] == 'win')}-"
                      f"{sum(1 for r in ats if r['ats_result'] == 'loss')}",
        'ats_pct': _pct(sum(1 for r in ats if r['ats_result'] == 'win'), len(ats)),
        'total_record': f"{sum(1 for r in totals if r['total_result'] == 'win')}-"
                        f"{sum(1 for r in totals if r['total_result'] == 'loss')}",
    }


def model_comparison(season=None) -> dict:
    """Graded performance split by the report model that wrote each report."""
    rows = history(season=season, graded_only=True)
    by_model: dict = {}
    for row in rows:
        by_model.setdefault(row.get('report_model') or 'unknown', []).append(row)
    return {'overall': _summarise(rows),
            'by_model': {model: _summarise(items)
                         for model, items in sorted(by_model.items())}}


def days_out_curve(season=None) -> list[dict]:
    """Does the prediction get better as game day approaches?

    Buckets every graded prediction by how many days before the game it ran. A
    healthy pipeline shows error falling as the bucket approaches zero — later
    information (injuries, market moves) should be worth something.
    """
    rows = [r for r in history(season=season, graded_only=True)
            if r.get('game_date') and r.get('margin_error') is not None]
    buckets: dict = {}
    for row in rows:
        game_day = _parse_dt(row['game_date'])
        run_day = _parse_dt(row['created_at'])
        if not game_day or not run_day:
            continue
        days = max(0, (game_day.date() - run_day.date()).days)
        label = '7+' if days >= 7 else str(days)
        buckets.setdefault(label, []).append(row)
    order = ['7+', '6', '5', '4', '3', '2', '1', '0']
    return [{'days_before_game': label, **_summarise(buckets[label])}
            for label in order if label in buckets]


def per_game_trajectories(season=None, limit: int = 12) -> list[dict]:
    """Games with multiple run-days: how the prediction moved as kickoff neared."""
    rows = history(season=season)
    by_game: dict = {}
    for row in rows:
        by_game.setdefault(_game_key(row), []).append(row)
    out = []
    for _key, series in by_game.items():
        if len(series) < 2:
            continue
        series.sort(key=lambda r: r['run_date'])
        latest = series[-1]
        out.append({
            'home': latest.get('home_short'), 'away': latest.get('away_short'),
            'season': latest.get('season'), 'week': latest.get('week'),
            'graded': bool(latest.get('graded')),
            'actual_margin': latest.get('actual_margin'),
            'runs': [{'run_date': r['run_date'],
                      'consensus_margin': r.get('consensus_margin'),
                      'market_margin': r.get('market_margin'),
                      'projected_total': r.get('projected_total'),
                      'home_win_probability': r.get('home_win_probability'),
                      'margin_error': r.get('margin_error')} for r in series],
        })
    out.sort(key=lambda g: (g['season'] or 0, g['week'] or 0), reverse=True)
    return out[:limit]
