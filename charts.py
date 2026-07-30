"""Procedurally generated report visuals.

Eight charts, the same eight on every report, built straight from the CFBD payloads and
the computed baseline. wkhtmltopdf runs an old WebKit with no usable JS or SVG support,
so each chart is rendered headless with matplotlib and embedded as a base64 PNG data URI.

Every builder is wrapped so a missing or malformed CFBD payload yields a styled
"no data" panel of identical dimensions rather than a broken report — the layout is
byte-for-byte consistent from one matchup to the next.
"""

import base64
import io
import logging
import math

import matplotlib
matplotlib.use("Agg")  # must precede pyplot; there is no display on the droplet

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

import cfbd
import config
import predict

FIG_W = 9.5
FIG_H = 4.6


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
def _apply_style():
    plt.rcParams.update({
        "figure.facecolor": config.CHART_FACE,
        "axes.facecolor": config.CHART_FACE,
        "axes.edgecolor": config.CHART_GRID,
        "axes.labelcolor": config.CHART_TEXT,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.titlecolor": config.CHART_TEXT,
        "axes.labelsize": 9,
        "xtick.color": config.CHART_MUTED,
        "ytick.color": config.CHART_MUTED,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 9,
        "legend.frameon": False,
        "grid.color": config.CHART_GRID,
        "grid.linewidth": 0.7,
        "font.size": 9,
    })


def _hex_ok(value: str) -> bool:
    v = (value or "").strip()
    if not v.startswith("#"):
        v = "#" + v
    return len(v) == 7 and all(c in "0123456789abcdefABCDEF" for c in v[1:])


def _norm_hex(value: str) -> str:
    v = (value or "").strip()
    return v if v.startswith("#") else "#" + v


def _rgb(hex_color: str):
    h = _norm_hex(hex_color).lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _distance(a: str, b: str) -> float:
    ra, rb = _rgb(a), _rgb(b)
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(ra, rb)))


def resolve_colors(home_meta: dict, away_meta: dict) -> tuple[str, str]:
    """Use each school's official color, falling back when missing or too similar."""
    home = _norm_hex(home_meta.get("color")) if _hex_ok(home_meta.get("color")) else config.CHART_FALLBACK_HOME
    away = _norm_hex(away_meta.get("color")) if _hex_ok(away_meta.get("color")) else config.CHART_FALLBACK_AWAY

    if _distance(home, away) < 70:
        alt = away_meta.get("alt_color")
        if _hex_ok(alt) and _distance(home, _norm_hex(alt)) >= 70:
            away = _norm_hex(alt)
        else:
            away = config.CHART_FALLBACK_AWAY
            if _distance(home, away) < 70:
                home = config.CHART_FALLBACK_HOME
    return home, away


def _encode(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=config.CHART_DPI, bbox_inches="tight",
                facecolor=config.CHART_FACE)
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _placeholder(title: str, message: str) -> str:
    _apply_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H / 1.9))
    ax.axis("off")
    ax.text(0.5, 0.62, title, ha="center", va="center", fontsize=12,
            fontweight="bold", color=config.CHART_TEXT)
    ax.text(0.5, 0.34, message, ha="center", va="center", fontsize=9.5, color=config.CHART_MUTED)
    ax.add_patch(plt.Rectangle((0.02, 0.08), 0.96, 0.84, fill=False,
                               edgecolor=config.CHART_GRID, linewidth=1.2, linestyle="--"))
    return _encode(fig)


def _grid(ax, axis="y"):
    ax.grid(True, axis=axis, alpha=0.6, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def _f(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _first(rows):
    return rows[0] if isinstance(rows, list) and rows else {}


def _paired_bars(ax, labels, home_vals, away_vals, home_c, away_c, home_l, away_l):
    """Vertical grouped bars with value labels; skips metrics where both sides are missing."""
    idx = [i for i, (h, a) in enumerate(zip(home_vals, away_vals)) if h is not None or a is not None]
    if not idx:
        return False
    labels = [labels[i] for i in idx]
    home_vals = [home_vals[i] if home_vals[i] is not None else 0 for i in idx]
    away_vals = [away_vals[i] if away_vals[i] is not None else 0 for i in idx]

    x = np.arange(len(labels))
    width = 0.38
    b1 = ax.bar(x - width / 2, home_vals, width, label=home_l, color=home_c, zorder=3)
    b2 = ax.bar(x + width / 2, away_vals, width, label=away_l, color=away_c, zorder=3)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=0)
    for bars in (b1, b2):
        ax.bar_label(bars, fmt="%.1f", padding=2, fontsize=7.5, color=config.CHART_TEXT)
    _grid(ax)
    return True


# ---------------------------------------------------------------------------
# 1. Power rating dashboard
# ---------------------------------------------------------------------------
def chart_power_ratings(stats, home_l, away_l, home_c, away_c):
    sp = stats.get("SP Ratings") or {}
    sp_h, sp_a = _first(sp.get("teamA")), _first(sp.get("teamB"))
    fpi = stats.get("FPI Ratings") or {}
    fpi_h, fpi_a = _first(fpi.get("teamA")), _first(fpi.get("teamB"))
    elo = stats.get("ELO Ratings") or {}
    elo_h, elo_a = _first(elo.get("teamA")), _first(elo.get("teamB"))

    def sp_part(row, part):
        node = row.get(part) if isinstance(row, dict) else None
        return _f(cfbd.pick(node, "rating")) if isinstance(node, dict) else None

    sp_labels = ["Overall", "Offense", "Defense\n(lower better)", "Special\nTeams"]
    sp_home = [_f(cfbd.pick(sp_h, "rating")), sp_part(sp_h, "offense"),
               sp_part(sp_h, "defense"), sp_part(sp_h, "specialTeams")]
    sp_away = [_f(cfbd.pick(sp_a, "rating")), sp_part(sp_a, "offense"),
               sp_part(sp_a, "defense"), sp_part(sp_a, "specialTeams")]

    fpi_home = [_f(cfbd.pick(fpi_h, "fpi"))]
    fpi_away = [_f(cfbd.pick(fpi_a, "fpi"))]
    elo_home = [_f(cfbd.pick(elo_h, "elo"))]
    elo_away = [_f(cfbd.pick(elo_a, "elo"))]

    if not any(v is not None for v in sp_home + sp_away + fpi_home + fpi_away + elo_home + elo_away):
        return None

    _apply_style()
    fig, axes = plt.subplots(1, 3, figsize=(FIG_W, FIG_H), gridspec_kw={"width_ratios": [3, 1, 1]})

    axes[0].set_title("SP+ Rating Components")
    if not _paired_bars(axes[0], sp_labels, sp_home, sp_away, home_c, away_c, home_l, away_l):
        axes[0].text(0.5, 0.5, "No SP+ data", ha="center", color=config.CHART_MUTED)
        axes[0].axis("off")
    axes[0].axhline(0, color=config.CHART_MUTED, linewidth=0.8)

    axes[1].set_title("FPI")
    if not _paired_bars(axes[1], ["FPI"], fpi_home, fpi_away, home_c, away_c, home_l, away_l):
        axes[1].text(0.5, 0.5, "No FPI data", ha="center", color=config.CHART_MUTED)
        axes[1].axis("off")

    axes[2].set_title("Elo")
    if not _paired_bars(axes[2], ["Elo"], elo_home, elo_away, home_c, away_c, home_l, away_l):
        axes[2].text(0.5, 0.5, "No Elo data", ha="center", color=config.CHART_MUTED)
        axes[2].axis("off")

    handles = [Patch(color=home_c, label=home_l), Patch(color=away_c, label=away_l)]
    fig.legend(handles=handles, loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.06))
    fig.suptitle("Power Rating Dashboard", fontsize=13, fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=(0, 0.02, 1, 0.94))
    return _encode(fig)


# ---------------------------------------------------------------------------
# 2. Efficiency radar (national percentiles)
# ---------------------------------------------------------------------------
def chart_efficiency_radar(percentiles, home_l, away_l, home_c, away_c):
    labels, home_vals, away_vals = [], [], []
    for label, data in (percentiles or {}).items():
        if data.get("home") is None and data.get("away") is None:
            continue
        labels.append(label.replace("Off. ", "O: ").replace("Def. ", "D: "))
        home_vals.append(data.get("home") if data.get("home") is not None else 50.0)
        away_vals.append(data.get("away") if data.get("away") is not None else 50.0)

    if len(labels) < 3:
        return None

    _apply_style()
    angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H + 1.4), subplot_kw={"polar": True})
    for vals, color, label in ((home_vals, home_c, home_l), (away_vals, away_c, away_l)):
        series = vals + vals[:1]
        ax.plot(angles, series, color=color, linewidth=2, label=label)
        ax.fill(angles, series, color=color, alpha=0.16)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylim(0, 100)
    ax.set_yticks([20, 40, 60, 80, 100])
    ax.set_yticklabels(["20th", "40th", "60th", "80th", "100th"], fontsize=7, color=config.CHART_MUTED)
    ax.set_rlabel_position(180 / max(len(labels), 1))
    ax.grid(color=config.CHART_GRID)
    ax.spines["polar"].set_color(config.CHART_GRID)
    # Title on the figure and legend below the plot, so neither can collide with the
    # spoke labels no matter how many axes the metric set produces.
    fig.suptitle("Efficiency Profile — National Percentile (outer ring is better)",
                 fontsize=13, fontweight="bold", color=config.CHART_TEXT, y=0.99)
    fig.legend(loc="lower center", ncol=2, bbox_to_anchor=(0.5, 0.005))
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    return _encode(fig)


# ---------------------------------------------------------------------------
# 3. Mismatch matrix
# ---------------------------------------------------------------------------
MISMATCH_PAIRS = [
    ("PPA / play",     "Off. PPA/play",        "Def. PPA/play"),
    ("Success Rate",   "Off. Success Rate",    "Def. Success Rate"),
    ("Explosiveness",  "Off. Explosiveness",   "Def. Explosiveness"),
    ("Line Yards",     "Off. Line Yards",      "Def. Line Yards"),
]


def _mismatch_panel(ax, title, edges, labels, pos_color, neg_color, pos_note, neg_note):
    y = np.arange(len(labels))
    colors = [pos_color if e >= 0 else neg_color for e in edges]
    ax.barh(y, edges, color=colors, height=0.55, zorder=3)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.axvline(0, color=config.CHART_MUTED, linewidth=1)
    ax.set_xlim(-100, 100)
    ax.set_xlabel("percentile edge")
    ax.set_title(title, fontsize=10)
    for yi, e in zip(y, edges):
        offset = 3 if e >= 0 else -3
        ax.text(e + offset, yi, f"{e:+.0f}", va="center",
                ha="left" if e >= 0 else "right", fontsize=8, color=config.CHART_TEXT)
    ax.text(0.99, -0.30, pos_note, transform=ax.transAxes, ha="right",
            fontsize=7.5, color=config.CHART_MUTED)
    ax.text(0.01, -0.30, neg_note, transform=ax.transAxes, ha="left",
            fontsize=7.5, color=config.CHART_MUTED)
    _grid(ax, axis="x")


def chart_mismatch_matrix(percentiles, home_l, away_l, home_c, away_c):
    if not percentiles:
        return None

    labels, home_edges, away_edges = [], [], []
    for label, off_key, def_key in MISMATCH_PAIRS:
        off = percentiles.get(off_key) or {}
        dfn = percentiles.get(def_key) or {}
        h_off, a_def = off.get("home"), dfn.get("away")
        a_off, h_def = off.get("away"), dfn.get("home")
        if h_off is None or a_def is None or a_off is None or h_def is None:
            continue
        labels.append(label)
        home_edges.append(h_off - a_def)
        away_edges.append(a_off - h_def)

    if not labels:
        return None

    _apply_style()
    fig, axes = plt.subplots(1, 2, figsize=(FIG_W, FIG_H), sharey=True)
    _mismatch_panel(axes[0], f"{home_l} offense  vs  {away_l} defense", home_edges, labels,
                    home_c, away_c, f"{home_l} edge →", f"← {away_l} edge")
    _mismatch_panel(axes[1], f"{away_l} offense  vs  {home_l} defense", away_edges, labels,
                    away_c, home_c, f"{away_l} edge →", f"← {home_l} edge")
    fig.suptitle("Mismatch Matrix — Unit-vs-Unit Percentile Edge", fontsize=13,
                 fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=(0, 0.06, 1, 0.93))
    return _encode(fig)


# ---------------------------------------------------------------------------
# 4. Season form trend
# ---------------------------------------------------------------------------
def _ppa_series(rows, side):
    points = []
    for r in rows or []:
        week = cfbd.pick(r, "week")
        node = r.get(side) if isinstance(r, dict) else None
        val = _f(cfbd.pick(node, "overall")) if isinstance(node, dict) else None
        if week is None or val is None:
            continue
        try:
            points.append((int(week), val))
        except (TypeError, ValueError):
            continue
    points.sort()
    return [p[0] for p in points], [p[1] for p in points]


def chart_form_trend(stats, home_l, away_l, home_c, away_c):
    ppa = stats.get("Team PPA") or {}
    series = {
        "offense": {
            "home": _ppa_series(ppa.get("teamA"), "offense"),
            "away": _ppa_series(ppa.get("teamB"), "offense"),
        },
        "defense": {
            "home": _ppa_series(ppa.get("teamA"), "defense"),
            "away": _ppa_series(ppa.get("teamB"), "defense"),
        },
    }
    if not any(s[0] for side in series.values() for s in side.values()):
        return None

    _apply_style()
    fig, axes = plt.subplots(1, 2, figsize=(FIG_W, FIG_H))
    titles = ("Offensive PPA by Week (higher is better)",
              "Defensive PPA Allowed by Week (lower is better)")
    for ax, side, title in zip(axes, ("offense", "defense"), titles):
        for who, color, label in (("home", home_c, home_l), ("away", away_c, away_l)):
            weeks, vals = series[side][who]
            if not weeks:
                continue
            ax.plot(weeks, vals, marker="o", markersize=4, linewidth=1.9, color=color, label=label)
        ax.axhline(0, color=config.CHART_MUTED, linewidth=0.8)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("week")
        ax.set_ylabel("PPA")
        _grid(ax)

    handles = [Patch(color=home_c, label=home_l), Patch(color=away_c, label=away_l)]
    fig.legend(handles=handles, loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.04))
    fig.suptitle("Season Form — Per-Game Predicted Points Added", fontsize=13,
                 fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=(0, 0.03, 1, 0.93))
    return _encode(fig)


# ---------------------------------------------------------------------------
# 5. Top player impact
# ---------------------------------------------------------------------------
def _top_players(rows, limit=8):
    ranked = cfbd.prune_player_ppa(rows, limit=limit)
    out = []
    for p in ranked:
        total = p.get("totalPPA")
        val = _f(cfbd.pick(total, "all")) if isinstance(total, dict) else _f(total)
        name = (cfbd.pick(p, "name", default="") or "").strip()
        pos = (cfbd.pick(p, "position", default="") or "").strip()
        if val is None or not name:
            continue
        out.append((f"{name} ({pos})" if pos else name, val))
    return out[:limit]


def chart_player_impact(stats, home_l, away_l, home_c, away_c):
    ppa = stats.get("Player PPA") or {}
    home_players = _top_players(ppa.get("teamA"))
    away_players = _top_players(ppa.get("teamB"))
    if not home_players and not away_players:
        return None

    _apply_style()
    fig, axes = plt.subplots(1, 2, figsize=(FIG_W, FIG_H + 0.5))
    for ax, players, color, label in ((axes[0], home_players, home_c, home_l),
                                      (axes[1], away_players, away_c, away_l)):
        if not players:
            ax.text(0.5, 0.5, "No player PPA data", ha="center", va="center",
                    color=config.CHART_MUTED, transform=ax.transAxes)
            ax.set_title(label, fontsize=10)
            ax.axis("off")
            continue
        names = [p[0] for p in players][::-1]
        vals = [p[1] for p in players][::-1]
        y = np.arange(len(names))
        ax.barh(y, vals, color=color, height=0.62, zorder=3)
        ax.set_yticks(y)
        ax.set_yticklabels(names, fontsize=7.5)
        ax.set_xlabel("total PPA")
        ax.set_title(label, fontsize=10)
        for yi, v in zip(y, vals):
            ax.text(v + (abs(max(vals, key=abs)) * 0.02 if vals else 0.1), yi, f"{v:.1f}",
                    va="center", fontsize=7.5, color=config.CHART_TEXT)
        _grid(ax, axis="x")

    fig.suptitle("Top Individual Impact — Season Total PPA", fontsize=13,
                 fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return _encode(fig)


# ---------------------------------------------------------------------------
# 6. Returning production & talent
# ---------------------------------------------------------------------------
def chart_returning_talent(stats, home_l, away_l, home_c, away_c):
    ret = stats.get("Returning Production") or {}
    r_h, r_a = _first(ret.get("teamA")), _first(ret.get("teamB"))
    tal = stats.get("Team Talent") or {}
    t_h, t_a = _first(tal.get("teamA")), _first(tal.get("teamB"))

    def pct(row, key):
        val = _f(cfbd.pick(row, key))
        if val is None:
            return None
        return val * 100.0 if abs(val) <= 1.0 else val

    labels = ["Total", "Passing", "Rushing", "Receiving"]
    keys = ["percentPPA", "percentPassingPPA", "percentRushingPPA", "percentReceivingPPA"]
    home_vals = [pct(r_h, k) for k in keys]
    away_vals = [pct(r_a, k) for k in keys]
    talent_home = [_f(cfbd.pick(t_h, "talent"))]
    talent_away = [_f(cfbd.pick(t_a, "talent"))]

    if not any(v is not None for v in home_vals + away_vals + talent_home + talent_away):
        return None

    _apply_style()
    fig, axes = plt.subplots(1, 2, figsize=(FIG_W, FIG_H), gridspec_kw={"width_ratios": [3, 1]})

    axes[0].set_title("Returning Production (% of prior-season PPA back)", fontsize=10)
    if _paired_bars(axes[0], labels, home_vals, away_vals, home_c, away_c, home_l, away_l):
        axes[0].set_ylabel("percent")
    else:
        axes[0].text(0.5, 0.5, "No returning production data", ha="center", color=config.CHART_MUTED)
        axes[0].axis("off")

    axes[1].set_title("247 Composite Talent", fontsize=10)
    if not _paired_bars(axes[1], ["Talent"], talent_home, talent_away, home_c, away_c, home_l, away_l):
        axes[1].text(0.5, 0.5, "No talent data", ha="center", color=config.CHART_MUTED)
        axes[1].axis("off")

    handles = [Patch(color=home_c, label=home_l), Patch(color=away_c, label=away_l)]
    fig.legend(handles=handles, loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.05))
    fig.suptitle("Roster Continuity & Recruiting Base", fontsize=13,
                 fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=(0, 0.02, 1, 0.93))
    return _encode(fig)


# ---------------------------------------------------------------------------
# 7. Projected margin by system
# ---------------------------------------------------------------------------
def chart_projection(baseline, home_l, away_l, home_c, away_c):
    rows = [(c["system"], c["margin"]) for c in (baseline.get("components") or [])]
    if baseline.get("market_margin") is not None:
        rows.append(("Market line", baseline["market_margin"]))
    if baseline.get("consensus_margin") is not None:
        rows.append(("CONSENSUS", baseline["consensus_margin"]))
    if not rows:
        return None

    _apply_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    labels = [r[0] for r in rows][::-1]
    values = [r[1] for r in rows][::-1]
    y = np.arange(len(labels))
    colors = [home_c if v >= 0 else away_c for v in values]
    bars = ax.barh(y, values, color=colors, height=0.55, zorder=3)
    for i, label in enumerate(labels):
        if label == "CONSENSUS":
            bars[i].set_edgecolor(config.CHART_TEXT)
            bars[i].set_linewidth(1.8)

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.axvline(0, color=config.CHART_TEXT, linewidth=1.1)
    span = max(abs(min(values)), abs(max(values)), 3) * 1.45
    ax.set_xlim(-span, span)
    ax.set_xlabel("projected margin (points)")
    for yi, v in zip(y, values):
        ax.text(v + (span * 0.03 if v >= 0 else -span * 0.03), yi, f"{v:+.1f}",
                va="center", ha="left" if v >= 0 else "right", fontsize=8.5,
                color=config.CHART_TEXT, fontweight="bold")
    ax.text(0.99, 1.02, f"{home_l} favored →", transform=ax.transAxes,
            ha="right", fontsize=8.5, color=home_c, fontweight="bold")
    ax.text(0.01, 1.02, f"← {away_l} favored", transform=ax.transAxes,
            ha="left", fontsize=8.5, color=away_c, fontweight="bold")
    _grid(ax, axis="x")
    ax.set_title("Projected Margin by Rating System", fontsize=13, pad=22)
    fig.tight_layout()
    return _encode(fig)


# ---------------------------------------------------------------------------
# 8. Win probability distribution
# ---------------------------------------------------------------------------
def chart_win_probability(baseline, home_l, away_l, home_c, away_c):
    margin = baseline.get("consensus_margin")
    if margin is None:
        return None
    sigma = baseline.get("margin_stddev") or config.MARGIN_STDDEV

    _apply_style()
    x = np.linspace(margin - 4 * sigma, margin + 4 * sigma, 600)
    y = np.exp(-0.5 * ((x - margin) / sigma) ** 2) / (sigma * math.sqrt(2 * math.pi))

    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ax.plot(x, y, color=config.CHART_TEXT, linewidth=1.4)
    ax.fill_between(x, y, where=(x >= 0), color=home_c, alpha=0.55, interpolate=True)
    ax.fill_between(x, y, where=(x < 0), color=away_c, alpha=0.55, interpolate=True)
    ax.axvline(0, color=config.CHART_TEXT, linewidth=1.1, linestyle="--")
    ax.axvline(margin, color=config.CHART_TEXT, linewidth=1.6)

    home_wp = predict.win_probability(margin, sigma) * 100
    ax.annotate(f"projection {margin:+.1f}", xy=(margin, max(y) * 0.98),
                xytext=(margin, max(y) * 1.10), ha="center", fontsize=9,
                fontweight="bold", color=config.CHART_TEXT)
    ax.text(0.985, 0.86, f"{home_l}\n{home_wp:.1f}%", transform=ax.transAxes, ha="right",
            fontsize=12, fontweight="bold", color=home_c)
    ax.text(0.015, 0.86, f"{away_l}\n{100 - home_wp:.1f}%", transform=ax.transAxes, ha="left",
            fontsize=12, fontweight="bold", color=away_c)

    ax.set_xlabel(f"final margin, {home_l} perspective (points)")
    ax.set_ylabel("probability density")
    ax.set_yticks([])
    ax.set_ylim(0, max(y) * 1.25)
    _grid(ax, axis="x")
    ax.set_title(f"Win Probability — margin uncertainty at σ = {sigma:g} points",
                 fontsize=13, pad=14)
    fig.tight_layout()
    return _encode(fig)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
CHART_SPECS = [
    ("power_ratings", "Power Rating Dashboard",
     "SP+, FPI and Elo side by side. SP+ and FPI are points-above-average ratings; the "
     "SP+ defensive rating counts down, so a lower bar is the better defense."),
    ("efficiency_radar", "Efficiency Profile",
     "Every advanced metric converted to a national percentile against all FBS teams, so the "
     "outer ring is always the better number regardless of which way the raw stat runs."),
    ("mismatch_matrix", "Mismatch Matrix",
     "Each offense measured directly against the defense it will face. Bars show the "
     "percentile gap; the longer the bar, the wider that specific unit-vs-unit edge."),
    ("form_trend", "Season Form Trend",
     "Week-by-week predicted points added. This is where you see a team trending up or "
     "falling apart, which season averages hide."),
    ("player_impact", "Top Individual Impact",
     "The players who have actually moved the needle, ranked by total PPA. Cross-reference "
     "this against the injury and roster sections."),
    ("returning_talent", "Roster Continuity & Talent",
     "How much production each team returned from last season, alongside the recruiting "
     "talent composite."),
    ("projection", "Projected Margin by System",
     "What each rating system independently projects, plus the market line and the blended "
     "consensus used as the model's anchor."),
    ("win_probability", "Win Probability Distribution",
     "The consensus margin spread across the historical error distribution for college "
     "football projections. The shaded area on each side is that team's win probability."),
]


def build_all(stats, percentiles, baseline, home_meta, away_meta, home_label, away_label) -> list[dict]:
    """Render all eight charts in fixed order. Failures become styled placeholders."""
    home_c, away_c = resolve_colors(home_meta, away_meta)

    builders = {
        "power_ratings": lambda: chart_power_ratings(stats, home_label, away_label, home_c, away_c),
        "efficiency_radar": lambda: chart_efficiency_radar(percentiles, home_label, away_label, home_c, away_c),
        "mismatch_matrix": lambda: chart_mismatch_matrix(percentiles, home_label, away_label, home_c, away_c),
        "form_trend": lambda: chart_form_trend(stats, home_label, away_label, home_c, away_c),
        "player_impact": lambda: chart_player_impact(stats, home_label, away_label, home_c, away_c),
        "returning_talent": lambda: chart_returning_talent(stats, home_label, away_label, home_c, away_c),
        "projection": lambda: chart_projection(baseline, home_label, away_label, home_c, away_c),
        "win_probability": lambda: chart_win_probability(baseline, home_label, away_label, home_c, away_c),
    }

    out: list[dict] = []
    for key, title, caption in CHART_SPECS:
        img, available = None, True
        try:
            img = builders[key]()
        except Exception as e:
            logging.warning(f"Chart '{key}' failed to render: {e}")
            img = None
        if not img:
            available = False
            img = _placeholder(title, "No data available from CollegeFootballData for this chart.")
        out.append({
            "key": key,
            "title": title,
            "caption": caption,
            "img": img,
            "available": available,
        })
    return out
