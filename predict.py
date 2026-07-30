"""Deterministic statistical baseline for the matchup.

The report is graded on how close its prediction lands to the real final score, and
models are far better at *adjusting* a numeric anchor for injuries and news than at
inventing a spread from prose. So we compute the anchor in Python from the CFBD
ratings, hand it to the report model, and chart it.

All margins in this module are expressed from the HOME team's perspective:
positive = home favored.
"""

import math

import config
import cfbd


def _f(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def win_probability(margin: float, sigma: float | None = None) -> float:
    """P(home wins) given a projected margin, using the CFB margin-error distribution."""
    sigma = sigma or config.MARGIN_STDDEV
    return normal_cdf(margin / sigma)


def _rating_components(stats: dict, hfa: float) -> list[dict]:
    """Per-system projected margins from SP+, FPI and Elo rating differentials."""
    components: list[dict] = []

    sp = stats.get("SP Ratings") or {}
    sp_home = _f(cfbd.pick(_first(sp.get("teamA")), "rating"))
    sp_away = _f(cfbd.pick(_first(sp.get("teamB")), "rating"))
    if sp_home is not None and sp_away is not None:
        components.append({
            "system": "SP+",
            "home_rating": round(sp_home, 2),
            "away_rating": round(sp_away, 2),
            "margin": round(sp_home - sp_away + hfa, 2),
        })

    fpi = stats.get("FPI Ratings") or {}
    fpi_home = _f(cfbd.pick(_first(fpi.get("teamA")), "fpi"))
    fpi_away = _f(cfbd.pick(_first(fpi.get("teamB")), "fpi"))
    if fpi_home is not None and fpi_away is not None:
        components.append({
            "system": "FPI",
            "home_rating": round(fpi_home, 2),
            "away_rating": round(fpi_away, 2),
            "margin": round(fpi_home - fpi_away + hfa, 2),
        })

    elo = stats.get("ELO Ratings") or {}
    elo_home = _f(cfbd.pick(_first(elo.get("teamA")), "elo"))
    elo_away = _f(cfbd.pick(_first(elo.get("teamB")), "elo"))
    if elo_home is not None and elo_away is not None:
        components.append({
            "system": "Elo",
            "home_rating": round(elo_home, 2),
            "away_rating": round(elo_away, 2),
            "margin": round((elo_home - elo_away) / config.ELO_POINTS_PER_MARGIN + hfa, 2),
        })

    return components


def _first(rows):
    return rows[0] if isinstance(rows, list) and rows else {}


def _projected_total(home_profile: dict, away_profile: dict) -> tuple[float, str]:
    """Expected combined points: each offense averaged against the other defense."""
    h_pf, h_pa = home_profile.get("ppg"), home_profile.get("papg")
    a_pf, a_pa = away_profile.get("ppg"), away_profile.get("papg")
    if None not in (h_pf, h_pa, a_pf, a_pa):
        est_home = (h_pf + a_pa) / 2.0
        est_away = (a_pf + h_pa) / 2.0
        return est_home + est_away, "season scoring rates (offense vs. opposing defense)"
    # No completed games yet (week 1, or a CFBD gap) — fall back to the FBS norm.
    return 55.0, "FBS scoring average (insufficient game data)"


def build_baseline(
    stats: dict,
    home_profile: dict,
    away_profile: dict,
    market: dict | None,
    home_short: str,
    away_short: str,
) -> dict:
    """Assemble the full quantitative anchor handed to the report model."""
    hfa = config.HOME_FIELD_ADVANTAGE
    components = _rating_components(stats, hfa)

    model_margin = None
    if components:
        model_margin = round(sum(c["margin"] for c in components) / len(components), 2)

    market_margin = (market or {}).get("market_margin_home")
    market_total = (market or {}).get("market_total")

    # The market line is the strongest single predictor available, so when it exists we
    # blend it 50/50 with the ratings consensus rather than ignoring either.
    if model_margin is not None and market_margin is not None:
        consensus = round((model_margin + market_margin) / 2.0, 2)
        consensus_basis = "50/50 blend of the ratings consensus and the market line"
    elif model_margin is not None:
        consensus = model_margin
        consensus_basis = "ratings consensus (no market line available)"
    elif market_margin is not None:
        consensus = market_margin
        consensus_basis = "market line (no ratings available)"
    else:
        consensus = None
        consensus_basis = "no rating or market data available"

    total, total_basis = _projected_total(home_profile, away_profile)
    if market_total:
        total = (total + market_total) / 2.0
        total_basis += " blended with the market total"

    projection = None
    if consensus is not None:
        home_score = total / 2.0 + consensus / 2.0
        away_score = total / 2.0 - consensus / 2.0
        projection = {
            "home_score": round(home_score, 1),
            "away_score": round(away_score, 1),
            "home_score_rounded": max(0, int(round(home_score))),
            "away_score_rounded": max(0, int(round(away_score))),
        }

    return {
        "perspective": f"Positive margin favors the HOME team ({home_short}); negative favors {away_short}.",
        "home_team": home_short,
        "away_team": away_short,
        "home_field_advantage_points": hfa,
        "components": components,
        "ratings_consensus_margin": model_margin,
        "market_margin": market_margin,
        "market_total": market_total,
        "market_providers": (market or {}).get("providers") or [],
        "consensus_margin": consensus,
        "consensus_basis": consensus_basis,
        "projected_total": round(total, 1),
        "projected_total_basis": total_basis,
        "projected_score": projection,
        "home_win_probability": round(win_probability(consensus) * 100, 1) if consensus is not None else None,
        "margin_stddev": config.MARGIN_STDDEV,
        "home_scoring_profile": home_profile,
        "away_scoring_profile": away_profile,
        "method": (
            "Each rating system's margin = (home rating - away rating) + home-field advantage; "
            f"Elo converted at {config.ELO_POINTS_PER_MARGIN} Elo points per point of margin. "
            "Win probability is the normal CDF of the consensus margin over a "
            f"{config.MARGIN_STDDEV}-point standard deviation."
        ),
    }
