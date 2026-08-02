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
import os
import tempfile

import cfbd
import config
import predict

# systemd units do not set HOME, so matplotlib's default cache path is unwritable under
# the service account. Point it somewhere writable BEFORE importing matplotlib, or every
# start pays a "created a temporary cache directory" penalty (or fails outright).
os.environ.setdefault(
    "MPLCONFIGDIR",
    os.path.join(os.getenv("XDG_CACHE_HOME") or tempfile.gettempdir(), "afplna-mpl"),
)
try:
    os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)
except Exception:
    pass

# A missing charting library must not take the whole service down with it — /get-report,
# /report-status and /health all have to keep working so the failure is diagnosable.
CHARTS_AVAILABLE = True
IMPORT_ERROR = ""
try:
    import matplotlib
    matplotlib.use("Agg")  # must precede pyplot; there is no display on the droplet

    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch
except Exception as _e:  # pragma: no cover - exercised only on a broken install
    CHARTS_AVAILABLE = False
    IMPORT_ERROR = f"{_e.__class__.__name__}: {_e}"
    logging.error(
        f"matplotlib unavailable ({IMPORT_ERROR}). Report charts are disabled; "
        f"run: pip install -r requirements.txt"
    )
    plt = None
    np = None
    Patch = None

FIG_W = 9.5
FIG_H = 4.6


class ChartsUnavailable(RuntimeError):
    """Raised when matplotlib could not be imported, so no chart can be produced."""


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
    if not CHARTS_AVAILABLE:
        raise ChartsUnavailable(
            f"matplotlib is not importable on this host ({IMPORT_ERROR}). "
            f"Install it with: pip install -r requirements.txt"
        )
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


# ---------------------------------------------------------------------------
# Single-team report visuals
# ---------------------------------------------------------------------------
def chart_team_results(games, team_label, color, alt_color):
    """Scoring margin by game — the season's shape at a glance."""
    played = [g for g in (games or [])
              if g.get("completed") and g.get("points_for") is not None
              and g.get("points_against") is not None]
    if not played:
        return None

    _apply_style()
    labels, margins = [], []
    for g in played:
        prefix = "vs" if g.get("home") else "@"
        labels.append(f'{prefix} {(g.get("opponent") or "?")[:14]}')
        margins.append(g["points_for"] - g["points_against"])

    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    x = np.arange(len(labels))
    # Wins in team color, losses muted — the win/loss split is the point of the chart.
    colors = [color if m > 0 else alt_color for m in margins]
    bars = ax.bar(x, margins, color=colors, width=0.62, zorder=3)
    ax.bar_label(bars, labels=[f"{m:+d}" for m in margins], padding=3,
                 fontsize=8, color=config.CHART_TEXT)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=8)
    ax.axhline(0, color=config.CHART_TEXT, linewidth=1.1)
    ax.set_ylabel("scoring margin")
    wins = sum(1 for m in margins if m > 0)
    ax.set_title(f"{team_label} — Margin by Game ({wins}-{len(margins) - wins})",
                 fontsize=13, pad=14)
    _grid(ax)
    fig.tight_layout()
    return _encode(fig)


def chart_team_radar(percentiles, team_label, color):
    """Single-team efficiency profile against the FBS field."""
    labels, values = [], []
    for label, data in (percentiles or {}).items():
        if data.get("home") is None:
            continue
        labels.append(label.replace("Off. ", "O: ").replace("Def. ", "D: "))
        values.append(data["home"])
    if len(labels) < 3:
        return None

    _apply_style()
    angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
    angles += angles[:1]
    series = values + values[:1]

    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H + 1.4), subplot_kw={"polar": True})
    ax.plot(angles, series, color=color, linewidth=2, label=team_label)
    ax.fill(angles, series, color=color, alpha=0.20)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylim(0, 100)
    ax.set_yticks([20, 40, 60, 80, 100])
    ax.set_yticklabels(["20th", "40th", "60th", "80th", "100th"], fontsize=7,
                       color=config.CHART_MUTED)
    ax.set_rlabel_position(180 / max(len(labels), 1))
    ax.grid(color=config.CHART_GRID)
    ax.spines["polar"].set_color(config.CHART_GRID)
    fig.suptitle(f"{team_label} — Efficiency Percentile (outer ring is better)",
                 fontsize=13, fontweight="bold", color=config.CHART_TEXT, y=0.99)
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    return _encode(fig)


def chart_team_form(ppa_rows, team_label, color, alt_color):
    """Per-game offensive and defensive PPA — trending up or falling apart."""
    off_weeks, off_vals = _ppa_series(ppa_rows, "offense")
    def_weeks, def_vals = _ppa_series(ppa_rows, "defense")
    if not off_weeks and not def_weeks:
        return None

    _apply_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    if off_weeks:
        ax.plot(off_weeks, off_vals, marker="o", markersize=5, linewidth=2,
                color=color, label="Offense (higher is better)")
    if def_weeks:
        ax.plot(def_weeks, def_vals, marker="s", markersize=5, linewidth=2,
                color=alt_color, linestyle="--", label="Defense allowed (lower is better)")
    ax.axhline(0, color=config.CHART_MUTED, linewidth=0.8)
    ax.set_xlabel("week")
    ax.set_ylabel("PPA per game")
    ax.legend(loc="best")
    ax.set_title(f"{team_label} — Predicted Points Added by Week", fontsize=13, pad=14)
    _grid(ax)
    fig.tight_layout()
    return _encode(fig)


def chart_team_players(player_rows, team_label, color):
    """Who is actually producing, ranked by season total PPA."""
    players = _top_players(player_rows, limit=12)
    if not players:
        return None

    _apply_style()
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H + 0.8))
    names = [p[0] for p in players][::-1]
    vals = [p[1] for p in players][::-1]
    y = np.arange(len(names))
    ax.barh(y, vals, color=color, height=0.66, zorder=3)
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=8)
    ax.set_xlabel("total PPA")
    span = abs(max(vals, key=abs)) if vals else 1
    for yi, v in zip(y, vals):
        ax.text(v + span * 0.02, yi, f"{v:.1f}", va="center", fontsize=8,
                color=config.CHART_TEXT)
    ax.set_title(f"{team_label} — Top Individual Impact (season total PPA)",
                 fontsize=13, pad=14)
    _grid(ax, axis="x")
    fig.tight_layout()
    return _encode(fig)


TEAM_CHART_SPECS = [
    ("team_results", "Season Results",
     "Scoring margin for every completed game. Wins are in the team's color, losses in "
     "the contrast color, so blowouts and near-misses are separable at a glance."),
    ("team_radar", "Efficiency Profile",
     "Every advanced metric converted to a national percentile against all FBS teams, so "
     "the outer ring is always better regardless of which way the raw stat runs."),
    ("team_form", "Form Trend",
     "Week-by-week predicted points added on both sides of the ball. This is where a team "
     "trending up or sliding shows through, which season averages hide."),
    ("team_players", "Top Individual Impact",
     "The players who have actually moved the needle this season. Cross-reference against "
     "the injury and roster sections."),
]


def build_team_charts(stats, percentiles, team_meta, team_label, games) -> list[dict]:
    """Render the four single-team charts. Failures become styled placeholders."""
    if not CHARTS_AVAILABLE:
        raise ChartsUnavailable(
            f"matplotlib is not importable on this host ({IMPORT_ERROR}). "
            f"Install it with: pip install -r requirements.txt"
        )

    color = (_norm_hex(team_meta.get("color")) if _hex_ok(team_meta.get("color"))
             else config.CHART_FALLBACK_HOME)
    alt = (_norm_hex(team_meta.get("alt_color")) if _hex_ok(team_meta.get("alt_color"))
           else config.CHART_FALLBACK_AWAY)
    # A team whose two official colors are near-identical (or black on black) needs a
    # readable contrast for the loss bars and the defensive line.
    if _distance(color, alt) < 70:
        alt = config.CHART_FALLBACK_AWAY if _distance(color, config.CHART_FALLBACK_AWAY) >= 70 \
            else config.CHART_MUTED

    builders = {
        "team_results": lambda: chart_team_results(games, team_label, color, alt),
        "team_radar": lambda: chart_team_radar(percentiles, team_label, color),
        "team_form": lambda: chart_team_form(stats.get("Team PPA"), team_label, color, alt),
        "team_players": lambda: chart_team_players(stats.get("Player PPA"), team_label, color),
    }

    out: list[dict] = []
    for key, title, caption in TEAM_CHART_SPECS:
        img, available = None, True
        try:
            img = builders[key]()
        except Exception as e:
            logging.warning(f"Team chart '{key}' failed to render: {e}")
            img = None
        if not img:
            available = False
            img = _placeholder(title, "No data available from CollegeFootballData for this chart.")
        out.append({"key": key, "title": title, "caption": caption,
                    "img": img, "available": available})
    return out


# ---------------------------------------------------------------------------
# Game recap charts
# ---------------------------------------------------------------------------
RECAP_CHART_SPECS = [
    ("recap_flow", "Scoring Flow",
     "The score after every scoring play, from kickoff to the final whistle. Long flat "
     "stretches are stalled offense; steep runs are momentum."),
    ("recap_drives", "Drive Outcomes",
     "Every drive by both offenses: how far it travelled and how it ended. Scoring "
     "drives are solid; empty possessions are hollow."),
    ("recap_box", "Advanced Box Score",
     "Efficiency, explosiveness and finishing for both offenses, from the advanced box "
     "score. Higher is better on every axis."),
    ("recap_players", "Top Individual Impact",
     "The players who moved the game most, by total PPA across their touches."),
]


def _drive_points(result: str) -> int | None:
    r = (result or "").upper()
    if "TD" in r:
        return 7
    if "FG" in r and "MISSED" not in r:
        return 3
    return 0


def chart_recap_flow(plays, game, home_c, away_c):
    home, away = game.get("homeTeam"), game.get("awayTeam")
    points = []
    for p in plays or []:
        try:
            period = int(p.get("period") or 0)
            clock = p.get("clock") or {}
            elapsed = (period - 1) * 900 + (900 - (int(clock.get("minutes") or 0) * 60
                                                   + int(clock.get("seconds") or 0)))
            off, home_score, away_score = p.get("offense"), None, None
            if off == home:
                home_score, away_score = p.get("offenseScore"), p.get("defenseScore")
            else:
                home_score, away_score = p.get("defenseScore"), p.get("offenseScore")
            if home_score is None or away_score is None:
                continue
            points.append((elapsed, int(home_score), int(away_score)))
        except (TypeError, ValueError):
            continue
    if len(points) < 4:
        return None
    points.sort()
    xs = [p[0] / 60 for p in points]
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ax.step(xs, [p[1] for p in points], where="post", color=home_c, linewidth=2.2,
            label=home, zorder=3)
    ax.step(xs, [p[2] for p in points], where="post", color=away_c, linewidth=2.2,
            label=away, zorder=3)
    top = max(max(p[1] for p in points), max(p[2] for p in points))
    for q in (15, 30, 45):
        ax.axvline(q, color=config.CHART_GRID, linewidth=0.9, linestyle="--", zorder=1)
    for q, x in enumerate((7.5, 22.5, 37.5, 52.5), start=1):
        ax.text(x, top * 1.06, f"Q{q}", ha="center", fontsize=8.5,
                color=config.CHART_MUTED)
    _grid(ax)
    ax.set_xlim(0, max(60, xs[-1]))
    ax.set_xlabel("game minute")
    ax.set_ylabel("points")
    ax.legend(loc="upper left", fontsize=9)
    fig.suptitle("Scoring Flow", fontsize=13, fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def chart_recap_drives(drives, game, home_c, away_c):
    home = game.get("homeTeam")
    rows = [d for d in drives or [] if d.get("offense")]
    if len(rows) < 4:
        return None
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    labels_done = set()
    for i, d in enumerate(rows):
        is_home = d.get("offense") == home
        color = home_c if is_home else away_c
        yards = d.get("yards") or 0
        scoring = bool(d.get("scoring"))
        label = d.get("offense") if d.get("offense") not in labels_done else None
        if label:
            labels_done.add(d.get("offense"))
        ax.bar(i + 1, yards, color=color, alpha=1.0 if scoring else 0.35,
               label=label, zorder=3)
    ax.axhline(0, color=config.CHART_MUTED, linewidth=0.8)
    _grid(ax)
    ax.set_xlabel("drive number (game order)")
    ax.set_ylabel("yards gained")
    ax.legend(loc="upper right", fontsize=9)
    fig.suptitle("Drive Outcomes — solid bars scored", fontsize=13,
                 fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def _box_team_rows(box: dict, section: str) -> list[dict]:
    teams = (box or {}).get("teams") or {}
    rows = teams.get(section)
    return rows if isinstance(rows, list) else []


def chart_recap_box(box, game, home_c, away_c):
    home, away = game.get("homeTeam"), game.get("awayTeam")
    ppa = {r.get("team"): r for r in _box_team_rows(box, "ppa")}
    rates = {r.get("team"): r for r in _box_team_rows(box, "successRates")}
    expl = {r.get("team"): r for r in _box_team_rows(box, "explosiveness")}
    opps = {r.get("team"): r for r in _box_team_rows(box, "scoringOpportunities")}

    def overall(table, team):
        row = table.get(team) or {}
        val = (row.get("overall") or {}).get("total")
        return float(val) if val is not None else None

    metrics = []
    for label, getter in (
        ("PPA/play", lambda t: overall(ppa, t)),
        ("Success rate", lambda t: overall(rates, t)),
        ("Explosiveness", lambda t: overall(expl, t)),
        ("Pts/opportunity", lambda t: (opps.get(t) or {}).get("pointsPerOpportunity")),
    ):
        hv, av = getter(home), getter(away)
        if hv is not None and av is not None:
            metrics.append((label, float(hv), float(av)))
    if len(metrics) < 2:
        return None

    fig, axes = plt.subplots(1, len(metrics), figsize=(FIG_W, FIG_H))
    if len(metrics) == 1:
        axes = [axes]
    for ax, (label, hv, av) in zip(axes, metrics):
        bars = ax.bar([0, 1], [hv, av], color=[home_c, away_c], zorder=3)
        ax.bar_label(bars, fmt="%.2f", padding=2, fontsize=8, color=config.CHART_TEXT)
        ax.set_xticks([])
        ax.set_title(label, fontsize=9.5, color=config.CHART_TEXT)
        _grid(ax)
    from matplotlib.patches import Patch
    fig.legend(handles=[Patch(color=home_c, label=home), Patch(color=away_c, label=away)],
               loc="lower center", ncol=2, fontsize=9, frameon=False)
    fig.suptitle("Advanced Box Score", fontsize=13, fontweight="bold",
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0.08, 1, 0.94])
    return _encode(fig)


def chart_recap_players(box, game, home_c, away_c):
    home = game.get("homeTeam")
    rows = ((box or {}).get("players") or {}).get("ppa") or []
    scored = []
    for r in rows:
        total = ((r.get("cumulative") or {}).get("total")
                 if isinstance(r.get("cumulative"), dict) else None)
        if total is None:
            total = (r.get("average") or {}).get("total") if isinstance(r.get("average"), dict) else None
        if total is None or not r.get("player"):
            continue
        scored.append((float(total), r))
    if len(scored) < 3:
        return None
    scored.sort(key=lambda t: abs(t[0]), reverse=True)
    top = scored[:10][::-1]
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    names = [f"{r.get('player')} ({r.get('position') or '?'}, {r.get('team')})"
             for _v, r in top]
    values = [v for v, _r in top]
    colors = [home_c if r.get("team") == home else away_c for _v, r in top]
    bars = ax.barh(names, values, color=colors, zorder=3)
    ax.bar_label(bars, fmt="%.1f", padding=2, fontsize=7.5, color=config.CHART_TEXT)
    _grid(ax, axis="x")
    ax.set_xlabel("total PPA in this game")
    fig.suptitle("Top Individual Impact", fontsize=13, fontweight="bold",
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def build_recap_charts(recap: dict, home_meta: dict, away_meta: dict) -> list[dict]:
    """Render the four game-recap charts. Failures become styled placeholders."""
    if not CHARTS_AVAILABLE:
        raise ChartsUnavailable(
            f"matplotlib is not importable on this host ({IMPORT_ERROR}). "
            f"Install it with: pip install -r requirements.txt"
        )
    _apply_style()
    game = recap.get("game") or {}
    home_c, away_c = resolve_colors(home_meta or {}, away_meta or {})

    builders = {
        "recap_flow": lambda: chart_recap_flow(recap.get("plays"), game, home_c, away_c),
        "recap_drives": lambda: chart_recap_drives(recap.get("drives"), game, home_c, away_c),
        "recap_box": lambda: chart_recap_box(recap.get("box"), game, home_c, away_c),
        "recap_players": lambda: chart_recap_players(recap.get("box"), game, home_c, away_c),
    }

    out: list[dict] = []
    for key, title, caption in RECAP_CHART_SPECS:
        img, available = None, True
        try:
            img = builders[key]()
        except Exception as e:
            logging.warning(f"Recap chart '{key}' failed to render: {e}")
            img = None
        if not img:
            available = False
            img = _placeholder(title, "No data available from CollegeFootballData for this chart.")
        out.append({"key": key, "title": title, "caption": caption,
                    "img": img, "available": available})
    return out


# ---------------------------------------------------------------------------
# Weekly publication charts
# ---------------------------------------------------------------------------
WEEKLY_CHART_SPECS = {
    'preview': [
        ("week_edges", "Model vs Market",
         "Every game with both a model margin and a market line. Distance from the "
         "diagonal is disagreement; the games farthest off it are the week's "
         "arguments."),
        ("week_mismatches", "Biggest Projected Mismatches",
         "The week's largest model margins — the games the numbers say should not be "
         "close."),
    ],
    'wrap': [
        ("week_movers", "Elo Movers",
         "The teams whose rating moved most on the week's results."),
        ("week_excitement", "The Best Games",
         "The week's finals ranked by excitement index."),
    ],
}


def chart_week_edges(games):
    pts = [(g['model_margin_home'], g['market_margin_home'],
            f"{g['away']} @ {g['home']}")
           for g in games
           if g.get('model_margin_home') is not None
           and g.get('market_margin_home') is not None]
    if len(pts) < 3:
        return None
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H + 0.8))
    xs = [p[1] for p in pts]
    ys = [p[0] for p in pts]
    lo = min(min(xs), min(ys)) - 3
    hi = max(max(xs), max(ys)) + 3
    ax.plot([lo, hi], [lo, hi], color=config.CHART_MUTED, linewidth=1,
            linestyle='--', zorder=2)
    ax.scatter(xs, ys, s=42, color=config.CHART_FALLBACK_HOME, zorder=3, alpha=0.85)
    ranked = sorted(pts, key=lambda p: -abs(p[0] - p[1]))[:6]
    for model, market, label in ranked:
        ax.annotate(label, (market, model), fontsize=7,
                    color=config.CHART_TEXT, xytext=(4, 4),
                    textcoords='offset points')
    _grid(ax, axis='both')
    ax.set_xlabel('market line (home margin)')
    ax.set_ylabel('model margin (home)')
    fig.suptitle('Model vs Market', fontsize=13, fontweight='bold',
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def chart_week_mismatches(games):
    rows = sorted((g for g in games if g.get('model_margin_home') is not None),
                  key=lambda g: -abs(g['model_margin_home']))[:12][::-1]
    if len(rows) < 3:
        return None
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H + 0.8))
    labels = [f"{g['away']} @ {g['home']}" for g in rows]
    values = [abs(g['model_margin_home']) for g in rows]
    bars = ax.barh(labels, values, color=config.CHART_FALLBACK_HOME, zorder=3)
    ax.bar_label(bars, fmt='%.1f', padding=2, fontsize=7.5, color=config.CHART_TEXT)
    _grid(ax, axis='x')
    ax.set_xlabel('projected margin (absolute points)')
    fig.suptitle('Biggest Projected Mismatches', fontsize=13, fontweight='bold',
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def chart_week_movers(finals):
    moves = []
    for g in finals or []:
        for side, pre, post in (('home', 'home_pregame_elo', 'home_postgame_elo'),
                                ('away', 'away_pregame_elo', 'away_postgame_elo')):
            if g.get(pre) is not None and g.get(post) is not None:
                moves.append((g[side], float(g[post]) - float(g[pre])))
    if len(moves) < 4:
        return None
    moves.sort(key=lambda m: m[1])
    picked = moves[:6] + moves[-6:]
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H + 0.8))
    labels = [m[0] for m in picked]
    values = [m[1] for m in picked]
    colors = [config.CHART_FALLBACK_AWAY if v < 0 else config.CHART_FALLBACK_HOME
              for v in values]
    bars = ax.barh(labels, values, color=colors, zorder=3)
    ax.bar_label(bars, fmt='%+.0f', padding=2, fontsize=7.5, color=config.CHART_TEXT)
    ax.axvline(0, color=config.CHART_MUTED, linewidth=0.8)
    _grid(ax, axis='x')
    ax.set_xlabel('Elo change this week')
    fig.suptitle('Elo Movers', fontsize=13, fontweight='bold', color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def chart_week_excitement(finals):
    rows = sorted((g for g in finals or [] if g.get('excitement_index') is not None),
                  key=lambda g: -float(g['excitement_index']))[:10][::-1]
    if len(rows) < 3:
        return None
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H + 0.8))
    labels = [f"{g['away']} {g['away_points']} @ {g['home']} {g['home_points']}"
              for g in rows]
    values = [float(g['excitement_index']) for g in rows]
    bars = ax.barh(labels, values, color=config.CHART_FALLBACK_HOME, zorder=3)
    ax.bar_label(bars, fmt='%.1f', padding=2, fontsize=7.5, color=config.CHART_TEXT)
    _grid(ax, axis='x')
    ax.set_xlabel('excitement index')
    fig.suptitle('The Best Games', fontsize=13, fontweight='bold',
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def build_weekly_charts(kind: str, data: dict, extras: dict) -> list[dict]:
    if not CHARTS_AVAILABLE:
        raise ChartsUnavailable(
            f"matplotlib is not importable on this host ({IMPORT_ERROR}). "
            f"Install it with: pip install -r requirements.txt")
    _apply_style()
    builders = {
        'week_edges': lambda: chart_week_edges(data['games']),
        'week_mismatches': lambda: chart_week_mismatches(data['games']),
        'week_movers': lambda: chart_week_movers(extras.get('finals_detail')),
        'week_excitement': lambda: chart_week_excitement(extras.get('finals_detail')),
    }
    out = []
    for key, title, caption in WEEKLY_CHART_SPECS[kind]:
        img, available = None, True
        try:
            img = builders[key]()
        except Exception as e:
            logging.warning(f"Weekly chart '{key}' failed to render: {e}")
            img = None
        if not img:
            available = False
            img = _placeholder(title, "No data available from CollegeFootballData for this chart.")
        out.append({"key": key, "title": title, "caption": caption,
                    "img": img, "available": available})
    return out


# ---------------------------------------------------------------------------
# Prediction performance charts
# ---------------------------------------------------------------------------
def chart_days_out(curve):
    rows = [c for c in curve or [] if c.get('mean_abs_error') is not None]
    if len(rows) < 2:
        return None
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    labels = [c['days_before_game'] for c in rows]
    values = [c['mean_abs_error'] for c in rows]
    ax.plot(labels, values, marker='o', color=config.CHART_FALLBACK_HOME,
            linewidth=2.2, zorder=3)
    for x, y in zip(labels, values):
        ax.annotate(f"{y:.1f}", (x, y), fontsize=8, color=config.CHART_TEXT,
                    xytext=(0, 7), textcoords='offset points', ha='center')
    _grid(ax)
    ax.set_xlabel('days before kickoff the prediction was made')
    ax.set_ylabel('mean absolute margin error (points)')
    fig.suptitle('Do Predictions Improve as Game Day Nears?', fontsize=13,
                 fontweight='bold', color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def chart_model_errors(by_model):
    rows = [(m, s['mean_abs_error']) for m, s in (by_model or {}).items()
            if s.get('mean_abs_error') is not None and s.get('graded', 0) >= 3]
    if len(rows) < 2:
        return None
    rows.sort(key=lambda r: r[1], reverse=True)
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    bars = ax.barh([r[0] for r in rows], [r[1] for r in rows],
                   color=config.CHART_FALLBACK_HOME, zorder=3)
    ax.bar_label(bars, fmt='%.1f', padding=2, fontsize=8, color=config.CHART_TEXT)
    _grid(ax, axis='x')
    ax.set_xlabel('mean absolute margin error (points) — lower is better')
    fig.suptitle('Report Model Comparison', fontsize=13, fontweight='bold',
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def chart_pred_vs_actual(rows):
    pts = [(float(r['consensus_margin']), int(r['actual_margin'])) for r in rows or []
           if r.get('consensus_margin') is not None and r.get('actual_margin') is not None]
    if len(pts) < 4:
        return None
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H + 0.6))
    lo = min(min(p[0] for p in pts), min(p[1] for p in pts)) - 4
    hi = max(max(p[0] for p in pts), max(p[1] for p in pts)) + 4
    ax.plot([lo, hi], [lo, hi], color=config.CHART_MUTED, linewidth=1,
            linestyle='--', zorder=2)
    ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=40,
               color=config.CHART_FALLBACK_HOME, alpha=0.8, zorder=3)
    _grid(ax, axis='both')
    ax.set_xlabel('predicted margin (home perspective)')
    ax.set_ylabel('actual margin')
    fig.suptitle('Predicted vs Actual Margins', fontsize=13, fontweight='bold',
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def build_prediction_charts(kind: str, *, curve, by_model, graded_rows) -> list[dict]:
    if not CHARTS_AVAILABLE:
        raise ChartsUnavailable(
            f"matplotlib is not importable on this host ({IMPORT_ERROR}). "
            f"Install it with: pip install -r requirements.txt")
    _apply_style()
    specs = [
        ("pred_scatter", "Predicted vs Actual Margins",
         "Every graded prediction against how the game actually finished. The tighter "
         "the cloud hugs the diagonal, the better the projections.",
         lambda: chart_pred_vs_actual(graded_rows)),
        ("pred_days_out", "Do Predictions Improve as Game Day Nears?",
         "Mean absolute margin error, bucketed by how many days before kickoff the "
         "prediction was made.",
         lambda: chart_days_out(curve)),
    ]
    if kind == 'audit':
        specs.append(
            ("pred_models", "Report Model Comparison",
             "Mean absolute margin error by the model that wrote each report — the "
             "measurable answer to which model earns its cost.",
             lambda: chart_model_errors(by_model)))
    out = []
    for key, title, caption, build in specs:
        img, available = None, True
        try:
            img = build()
        except Exception as e:
            logging.warning(f"Prediction chart '{key}' failed to render: {e}")
            img = None
        if not img:
            available = False
            img = _placeholder(title, "Not enough graded predictions yet for this chart.")
        out.append({"key": key, "title": title, "caption": caption,
                    "img": img, "available": available})
    return out
