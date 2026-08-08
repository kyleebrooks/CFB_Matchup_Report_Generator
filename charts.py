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


def _luminance(hex_color: str) -> float:
    r, g, b = _rgb(hex_color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _shade(hex_color: str, factor: float) -> str:
    """Darken (<1) or lighten (>1) a color while keeping its hue recognisable."""
    r, g, b = _rgb(hex_color)
    if factor <= 1:
        r, g, b = (int(c * factor) for c in (r, g, b))
    else:
        blend = min(1.0, factor - 1.0)
        r, g, b = (int(c + (255 - c) * blend) for c in (r, g, b))
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def _visible(hex_color: str) -> str:
    """The color, darkened just enough to survive a white page. A school whose
    primary is white/cream (or whose alt got picked) must never paint invisible bars."""
    color = _norm_hex(hex_color)
    while _luminance(color) > 0.82:
        darker = _shade(color, 0.8)
        if darker == color:            # pure white cannot darken multiplicatively
            return config.CHART_MUTED
        color = darker
    return color


def resolve_colors(home_meta: dict, away_meta: dict) -> tuple[str, str]:
    """Each school's ACTUAL base color, kept whenever possible.

    The old behaviour swapped a team to a generic navy/red the moment the two
    colors were similar — so Georgia vs Alabama rendered in stock colors and the
    charts stopped looking like either school. Now similarity is resolved by
    shading the away color (still recognisably theirs) or by its alternate, and a
    too-light color is darkened rather than replaced. Generic fallbacks are the
    last resort, not the first."""
    home = _visible(home_meta.get("color")) if _hex_ok(home_meta.get("color")) \
        else config.CHART_FALLBACK_HOME
    away = _visible(away_meta.get("color")) if _hex_ok(away_meta.get("color")) \
        else config.CHART_FALLBACK_AWAY

    if _distance(home, away) >= 70:
        return home, away

    # Too similar. Try, in order of how much identity each option keeps:
    # the away alternate color, a darker shade of away, a lighter shade of away.
    alt = away_meta.get("alt_color")
    # Only a REAL alternate color qualifies — most alternates are white/cream, and
    # force-darkening those yields a washed gray when a rich shade of the school's
    # own primary is available below.
    if _hex_ok(alt) and _luminance(_norm_hex(alt)) <= 0.82:
        alt = _visible(alt)
        if _distance(home, alt) >= 70:
            return home, alt
    for factor in (0.55, 1.55):
        shaded = _visible(_shade(away, factor))
        if _distance(home, shaded) >= 70:
            return home, shaded
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
def chart_verdict(baseline, home_l, away_l, home_c, away_c):
    """The reveal: a scoreboard-style card for the final prediction.

    Projected score in each school's color, the win-probability split, and the
    model's spread set against the market — the numbers the whole report builds
    to, presented like a result rather than buried in prose."""
    projection = (baseline or {}).get("projected_score") or {}
    home_pts, away_pts = _f(projection.get("home_score")), _f(projection.get("away_score"))
    prob = _f((baseline or {}).get("home_win_probability"))
    if home_pts is None or away_pts is None or prob is None:
        return None
    consensus = _f(baseline.get("consensus_margin"))
    market = _f(baseline.get("market_margin"))
    total = _f(baseline.get("projected_total"))
    market_total = _f(baseline.get("market_total"))

    fig, ax = plt.subplots(figsize=(FIG_W, 5.4))
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    ax.text(0.5, 0.97, "PROJECTED FINAL", ha="center", va="top", fontsize=13,
            fontweight="bold", color=config.CHART_TEXT)

    # The scoreboard: each side in its own color, the projected winner's panel solid.
    home_wins = home_pts >= away_pts
    for x, label, pts, color, wins in ((0.26, home_l, home_pts, home_c, home_wins),
                                       (0.74, away_l, away_pts, away_c, not home_wins)):
        panel = plt.Rectangle((x - 0.21, 0.56), 0.42, 0.30,
                              facecolor=color if wins else "none",
                              edgecolor=color, linewidth=2.5, zorder=2)
        ax.add_patch(panel)
        ax.text(x, 0.795, label, ha="center", va="center", fontsize=12.5,
                fontweight="bold", color="white" if wins else color, zorder=3)
        ax.text(x, 0.665, f"{pts:.0f}", ha="center", va="center", fontsize=30,
                fontweight="bold", color="white" if wins else color, zorder=3)
    ax.text(0.5, 0.71, "—", ha="center", va="center", fontsize=16,
            color=config.CHART_MUTED)

    # Win probability, one bar split in team colors.
    bar_y, bar_h = 0.40, 0.075
    ax.add_patch(plt.Rectangle((0.05, bar_y), 0.90 * prob, bar_h,
                               facecolor=home_c, zorder=2))
    ax.add_patch(plt.Rectangle((0.05 + 0.90 * prob, bar_y), 0.90 * (1 - prob), bar_h,
                               facecolor=away_c, zorder=2))
    ax.text(0.05, bar_y + bar_h + 0.015, f"{home_l}  {prob * 100:.0f}%",
            ha="left", va="bottom", fontsize=10, fontweight="bold", color=home_c)
    ax.text(0.95, bar_y + bar_h + 0.015, f"{(1 - prob) * 100:.0f}%  {away_l}",
            ha="right", va="bottom", fontsize=10, fontweight="bold", color=away_c)
    ax.text(0.5, bar_y - 0.045, "WIN PROBABILITY", ha="center", va="top",
            fontsize=8, color=config.CHART_MUTED)

    # The chips: spread, market and edge, total.
    def spread_text(margin):
        if margin is None:
            return "—"
        fav = home_l if margin > 0 else away_l
        return "PICK 'EM" if abs(margin) < 0.05 else f"{fav} -{abs(margin):.1f}"

    chips = [("MODEL SPREAD", spread_text(consensus))]
    if market is not None and consensus is not None:
        edge = consensus - market
        chips.append(("MARKET", f"{spread_text(market)}  (edge {edge:+.1f})"))
    if total is not None:
        text = f"{total:.0f}" + (f"  (market {market_total:.0f})" if market_total else "")
        chips.append(("PROJECTED TOTAL", text))
    width = 0.9 / len(chips)
    for i, (label, value) in enumerate(chips):
        cx = 0.05 + width * (i + 0.5)
        ax.add_patch(plt.Rectangle((cx - width / 2 + 0.01, 0.10), width - 0.02, 0.17,
                                   fill=False, edgecolor=config.CHART_GRID,
                                   linewidth=1.2, zorder=2))
        ax.text(cx, 0.225, label, ha="center", va="center", fontsize=7.5,
                color=config.CHART_MUTED)
        ax.text(cx, 0.155, value, ha="center", va="center", fontsize=10.5,
                fontweight="bold", color=config.CHART_TEXT)

    basis = (baseline or {}).get("consensus_basis") or ""
    if basis:
        ax.text(0.5, 0.02, f"Consensus margin: {basis}.", ha="center", va="bottom",
                fontsize=7.5, color=config.CHART_MUTED, style="italic")
    fig.tight_layout()
    return _encode(fig)


CHART_SPECS = [
    ("conditions", "Game Conditions",
     "The forecast and the ground it will be played on: temperature, wind and "
     "precipitation beside the stadium's surface, roof, capacity and elevation."),
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


def build_all(stats, percentiles, baseline, home_meta, away_meta, home_label, away_label,
              conditions=None) -> list[dict]:
    """Render all the matchup charts in fixed order. Failures become styled placeholders."""
    if not CHARTS_AVAILABLE:
        raise ChartsUnavailable(
            f"matplotlib is not importable on this host ({IMPORT_ERROR}). "
            f"Install it with: pip install -r requirements.txt"
        )
    home_c, away_c = resolve_colors(home_meta, away_meta)

    builders = {
        "conditions": lambda: chart_conditions(conditions, home_c),
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

    # The ninth chart is the reveal: it renders WITH the Final Prediction section at
    # the end of the report (placement="finale"), not in the mid-report gallery.
    verdict_img = None
    try:
        verdict_img = chart_verdict(baseline, home_label, away_label, home_c, away_c)
    except Exception as e:
        logging.warning(f"Chart 'verdict' failed to render: {e}")
    if verdict_img:
        out.append({
            "key": "verdict",
            "title": "The Verdict",
            "caption": ("The projected final score, win probability and the model's "
                        "spread against the market — the report's bottom line in one "
                        "card."),
            "img": verdict_img,
            "available": True,
            "placement": "finale",
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
    ("team_road", "The Road Ahead",
     "Projected margin for every remaining game, from the SP+/FPI/Elo consensus with "
     "home-field advantage — the same baseline the matchup reports use. Bars below "
     "zero are games the ratings say this team should lose."),
]


def chart_team_road(outlook, team_label, color, alt):
    """Projected margin per remaining game; negative bars are projected losses."""
    games = [g for g in (outlook or {}).get("games") or []
             if g.get("projected_margin") is not None]
    if len(games) < 2:
        return None
    labels = []
    for g in games:
        site = {"home": "vs", "away": "at", "neutral": "n."}.get(g.get("site"), "vs")
        rank = f" (#{g['opponent_sp_rank']} SP+)" if g.get("opponent_sp_rank") and \
            g["opponent_sp_rank"] <= 25 else ""
        labels.append(f"Wk {g.get('week') or '?'} {site} {g.get('opponent')}{rank}")
    values = [float(g["projected_margin"]) for g in games]

    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    bars = ax.bar(range(len(games)), values,
                  color=[color if v >= 0 else alt for v in values], zorder=3)
    ax.bar_label(bars, labels=[f"{v:+.1f}" for v in values], padding=2,
                 fontsize=8, color=config.CHART_TEXT)
    ax.axhline(0, color=config.CHART_TEXT, linewidth=0.8)
    ax.set_xticks(range(len(games)))
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=7.5)
    ax.set_ylabel(f"projected margin for {team_label}")
    _grid(ax)
    fig.suptitle("The Road Ahead", fontsize=13, fontweight="bold",
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def build_team_charts(stats, percentiles, team_meta, team_label, games,
                      outlook=None) -> list[dict]:
    """Render the five single-team charts. Failures become styled placeholders."""
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
        "team_road": lambda: chart_team_road(outlook, team_label, color, alt),
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
    ("recap_conditions", "Game Conditions",
     "What the game was played in and on: the day's weather beside the stadium's "
     "surface, roof, capacity and elevation."),
    ("recap_flow", "Scoring Flow",
     "The score after every scoring play, from kickoff to the final whistle. Long flat "
     "stretches are stalled offense; steep runs are momentum."),
    ("recap_winprob", "Win Probability",
     "The home team's chance of winning after every play. Cliffs are the game's true "
     "turning points; a line that hugs one edge is a game that was never close. For "
     "games CFBD's model skipped, the curve is estimated from score, clock, "
     "possession and the pregame spread, and labeled as such."),
    ("recap_drives", "Drive Outcomes",
     "Every drive by both offenses: how far it travelled and how it ended. Scoring "
     "drives are solid; empty possessions are hollow."),
    ("recap_box", "Advanced Box Score",
     "Efficiency, explosiveness and finishing for both offenses, from the advanced box "
     "score. Higher is better on every axis."),
    ("recap_players", "Top Individual Impact",
     "The players who moved the game most, by total PPA across their touches."),
    ("recap_playtypes", "Play-Type Success",
     "Success rate by play group — rushes, passes (all attempts together) and sacks "
     "— side by side. The gap between a team's rush and pass bars is the story of "
     "its play-calling night."),
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


def chart_recap_winprob(wp_rows, game, home_c, away_c, estimated=False):
    """The home team's win probability after every play — the story of the game."""
    home, away = game.get("homeTeam"), game.get("awayTeam")
    rows = [r for r in wp_rows or [] if r.get("homeWinProbability") is not None]
    if len(rows) < 12:
        return None
    rows.sort(key=lambda r: int(r.get("playNumber") or 0))
    y = [float(r["homeWinProbability"]) * 100 for r in rows]
    x = list(range(len(y)))

    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ax.axhline(50, color=config.CHART_MUTED, linewidth=1, linestyle="--", zorder=2)
    ax.fill_between(x, y, 50, where=[v >= 50 for v in y],
                    color=home_c, alpha=0.16, zorder=1, interpolate=True)
    ax.fill_between(x, y, 50, where=[v <= 50 for v in y],
                    color=away_c, alpha=0.16, zorder=1, interpolate=True)
    ax.plot(x, y, color=home_c, linewidth=2.2, zorder=4)
    ax.set_ylim(0, 100)
    ax.set_xlim(0, len(y) - 1)
    ax.set_xlabel("play number")
    ax.set_ylabel(f"{home} win probability, %")
    ax.text(0.01, 0.96, f"{home} territory", transform=ax.transAxes,
            fontsize=8, color=home_c, va="top")
    ax.text(0.01, 0.05, f"{away} territory", transform=ax.transAxes,
            fontsize=8, color=away_c, va="bottom")
    _grid(ax)
    title = "Win Probability (model estimate)" if estimated else "Win Probability"
    if estimated:
        ax.text(0.99, 0.03, "estimated from score, clock, possession and the "
                            "pregame spread — CFBD stores no series for this game",
                transform=ax.transAxes, ha="right", va="bottom", fontsize=7.5,
                color=config.CHART_MUTED)
    fig.suptitle(title, fontsize=13, fontweight="bold", color=config.CHART_TEXT)
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


def chart_recap_playtypes(playtypes, game, home_c, away_c):
    """Success rate by play type, both offenses side by side."""
    home, away = game.get("homeTeam"), game.get("awayTeam")
    if not playtypes or home not in playtypes or away not in playtypes:
        return None

    def rows(team):
        return {r["group"]: r for r in playtypes[team].get("offense_play_groups") or []
                if r.get("plays", 0) >= 3}

    home_rows, away_rows = rows(home), rows(away)
    types = sorted(set(home_rows) | set(away_rows),
                   key=lambda t: -((home_rows.get(t) or {}).get("plays", 0)
                                   + (away_rows.get(t) or {}).get("plays", 0)))[:7]
    if len(types) < 2:
        return None

    import numpy as np
    y = np.arange(len(types))
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    hv = [(home_rows.get(t) or {}).get("success_rate") or 0 for t in types]
    av = [(away_rows.get(t) or {}).get("success_rate") or 0 for t in types]
    bars_h = ax.barh(y + 0.2, hv, height=0.38, color=home_c, zorder=3, label=home)
    bars_a = ax.barh(y - 0.2, av, height=0.38, color=away_c, zorder=3, label=away)
    for bars, vals, side in ((bars_h, hv, home_rows), (bars_a, av, away_rows)):
        labels = [f"{v:.0f}%  ({(side.get(t) or {}).get('plays', 0)})"
                  for v, t in zip(vals, types)]
        ax.bar_label(bars, labels=labels, padding=3, fontsize=7.5,
                     color=config.CHART_TEXT)
    ax.set_yticks(y)
    ax.set_yticklabels(types, fontsize=8.5)
    ax.invert_yaxis()
    ax.set_xlim(0, 108)
    ax.set_xlabel("success rate, % (plays run in parentheses)")
    _grid(ax, axis="x")
    ax.legend(loc="lower right", fontsize=8.5, frameon=False)
    fig.suptitle("Play-Type Success", fontsize=13, fontweight="bold",
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def build_recap_charts(recap: dict, home_meta: dict, away_meta: dict,
                       playtypes: dict | None = None,
                       conditions: dict | None = None) -> list[dict]:
    """Render the game-recap charts. Failures become styled placeholders."""
    if not CHARTS_AVAILABLE:
        raise ChartsUnavailable(
            f"matplotlib is not importable on this host ({IMPORT_ERROR}). "
            f"Install it with: pip install -r requirements.txt"
        )
    _apply_style()
    game = recap.get("game") or {}
    home_c, away_c = resolve_colors(home_meta or {}, away_meta or {})

    builders = {
        "recap_conditions": lambda: chart_conditions(conditions, home_c),
        "recap_flow": lambda: chart_recap_flow(recap.get("plays"), game, home_c, away_c),
        "recap_winprob": lambda: chart_recap_winprob(
            recap.get("wp"), game, home_c, away_c,
            estimated=bool(recap.get("wp_estimated"))),
        "recap_drives": lambda: chart_recap_drives(recap.get("drives"), game, home_c, away_c),
        "recap_box": lambda: chart_recap_box(recap.get("box"), game, home_c, away_c),
        "recap_players": lambda: chart_recap_players(recap.get("box"), game, home_c, away_c),
        "recap_playtypes": lambda: chart_recap_playtypes(playtypes, game, home_c, away_c),
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


# ---------------------------------------------------------------------------
# Full season play-by-play charts
# ---------------------------------------------------------------------------
SEASON_PLAY_CHART_SPECS = [
    ("season_directions", "The Ground Game",
     "Where the runs went when the play text names the gap; when it rarely does "
     "(the norm in recent seasons), the designed-rush outcome distribution "
     "instead — losses through breakaways — which needs no play text at all."),
    ("season_downs", "Success by Down",
     "Offensive success rate on each down, next to what the defense allowed. The "
     "run-share line shows how predictable the play-calling became as downs got "
     "longer."),
    ("season_third", "Third Down by Distance",
     "Conversion rate on 3rd-and-short, medium and long — the offense's rate "
     "beside the defense's rate allowed. Money-down execution, both sides."),
    ("season_trend", "Week-to-Week Evolution",
     "Offensive and defensive success rate game by game across the season. "
     "Diverging lines are a team changing; parallel lines are a team being who "
     "it is."),
]


def chart_season_directions(breakdown, team_label, color, alt):
    """The ground game, told honestly.

    When the play text names gaps often enough (≥25% of designed rushes), this is
    the direction chart. When it does not — the norm in recent seasons — direction
    bars would chart parsing luck, not tendencies, so the chart becomes the
    designed-rush outcome distribution instead, which needs no play text at all.
    """
    situational = (breakdown.get("offense") or {}).get("situational") or {}
    coverage = situational.get("rush_direction_coverage") or {}
    covered = (coverage.get("classified_pct") or 0) >= 25

    if covered:
        directions = situational.get("rush_directions") or {}
        order = ["left end", "left tackle", "left guard", "middle",
                 "right guard", "right tackle", "right end", "unclassified"]
        rows = [(d, directions[d]) for d in order if d in directions]
        if sum(r["plays"] for _d, r in rows) < 10:
            return None
        fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
        names = [d for d, _r in rows][::-1]
        counts = [r["plays"] for _d, r in rows][::-1]
        rates = [r.get("success_rate") or 0 for _d, r in rows][::-1]
        colors = [alt if d == "unclassified" else color for d in names]
        bars = ax.barh(names, counts, color=colors, zorder=3)
        labels = [f"{c} carries — {s:.0f}% success" for c, s in zip(counts, rates)]
        ax.bar_label(bars, labels=labels, padding=3, fontsize=8, color=config.CHART_TEXT)
        ax.set_xlim(0, max(counts) * 1.45)
        ax.set_xlabel("designed rushes this season")
        _grid(ax, axis="x")
        fig.suptitle(f"{team_label}: Where the Runs Went", fontsize=13,
                     fontweight="bold", color=config.CHART_TEXT)
        fig.tight_layout(rect=[0, 0, 1, 0.94])
        return _encode(fig)

    outcomes = situational.get("designed_rush_outcomes") or {}
    bands = [("loss", "loss"), ("no_gain", "no gain"), ("short_1_3", "1–3 yds"),
             ("solid_4_9", "4–9 yds"), ("chunk_10_14", "10–14 yds"),
             ("breakaway_15plus", "15+ yds")]
    counts = [outcomes.get(k, 0) for k, _l in bands]
    total = sum(counts)
    if total < 10:
        return None
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    shades = [_shade(color, f) for f in (0.45, 0.65, 0.85, 1.0, 1.2, 1.45)]
    bars = ax.bar([l for _k, l in bands], counts, color=shades, zorder=3)
    ax.bar_label(bars, labels=[f"{c}\n{c / total * 100:.0f}%" for c in counts],
                 padding=3, fontsize=8.5, color=config.CHART_TEXT)
    ax.set_ylim(0, max(counts) * 1.3)
    ax.set_ylabel("designed rushes")
    _grid(ax)
    ax.text(0.99, 0.95,
            f"play text names a gap on only "
            f"{coverage.get('classified_pct') or 0:.0f}% of rushes —\n"
            f"outcomes shown instead of directions",
            transform=ax.transAxes, ha="right", va="top", fontsize=8,
            color=config.CHART_MUTED)
    fig.suptitle(f"{team_label}: The Ground Game, by Outcome", fontsize=13,
                 fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def chart_season_downs(breakdown, team_label, color, alt):
    off = ((breakdown.get("offense") or {}).get("situational") or {}).get("by_down") or {}
    deff = ((breakdown.get("defense_allowed") or {}).get("situational") or {}) \
        .get("by_down") or {}
    downs = [d for d in ("1", "2", "3", "4") if d in off]
    if len(downs) < 3:
        return None
    x = np.arange(len(downs))
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ov = [off[d].get("success_rate") or 0 for d in downs]
    dv = [(deff.get(d) or {}).get("success_rate") or 0 for d in downs]
    bars_o = ax.bar(x - 0.18, ov, width=0.34, color=color, zorder=3,
                    label="offense success")
    bars_d = ax.bar(x + 0.18, dv, width=0.34, color=alt, zorder=3,
                    label="defense allowed")
    ax.bar_label(bars_o, fmt="%.0f%%", padding=2, fontsize=8, color=config.CHART_TEXT)
    ax.bar_label(bars_d, fmt="%.0f%%", padding=2, fontsize=8, color=config.CHART_TEXT)
    share = [off[d].get("rush_share_pct") or 0 for d in downs]
    ax.plot(x, share, marker="o", linewidth=2, color=config.CHART_TEXT,
            zorder=4, label="offense run share")
    for xi, s in zip(x, share):
        ax.annotate(f"{s:.0f}%", (xi, s), textcoords="offset points",
                    xytext=(0, 7), ha="center", fontsize=7.5,
                    color=config.CHART_TEXT)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{d}{'st' if d=='1' else 'nd' if d=='2' else 'rd' if d=='3' else 'th'} down"
                        for d in downs])
    ax.set_ylim(0, 100)
    ax.set_ylabel("%")
    _grid(ax)
    ax.legend(loc="upper right", fontsize=8.5, frameon=False)
    fig.suptitle(f"{team_label}: Success by Down", fontsize=13,
                 fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def chart_season_third(breakdown, team_label, color, alt):
    off = ((breakdown.get("offense") or {}).get("situational") or {}) \
        .get("third_down_by_distance") or {}
    deff = ((breakdown.get("defense_allowed") or {}).get("situational") or {}) \
        .get("third_down_by_distance") or {}
    buckets = [("short_1_3", "3rd & 1-3"), ("medium_4_6", "3rd & 4-6"),
               ("long_7plus", "3rd & 7+")]
    present = [(k, label) for k, label in buckets if k in off]
    if len(present) < 2:
        return None
    x = np.arange(len(present))
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ov = [off[k].get("conversion_pct") or 0 for k, _l in present]
    dv = [(deff.get(k) or {}).get("conversion_pct") or 0 for k, _l in present]
    bars_o = ax.bar(x - 0.18, ov, width=0.34, color=color, zorder=3, label="offense converts")
    bars_d = ax.bar(x + 0.18, dv, width=0.34, color=alt, zorder=3, label="defense allows")
    labels_o = [f"{v:.0f}% ({off[k]['attempts']})" for v, (k, _l) in zip(ov, present)]
    labels_d = [f"{v:.0f}% ({(deff.get(k) or {}).get('attempts', 0)})"
                for v, (k, _l) in zip(dv, present)]
    ax.bar_label(bars_o, labels=labels_o, padding=2, fontsize=8, color=config.CHART_TEXT)
    ax.bar_label(bars_d, labels=labels_d, padding=2, fontsize=8, color=config.CHART_TEXT)
    ax.set_xticks(x)
    ax.set_xticklabels([label for _k, label in present])
    ax.set_ylim(0, 100)
    ax.set_ylabel("conversion rate, % (attempts in parentheses)")
    _grid(ax)
    ax.legend(loc="upper right", fontsize=8.5, frameon=False)
    fig.suptitle(f"{team_label}: Third Down by Distance", fontsize=13,
                 fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def chart_season_trend(game_log, team_label, color, alt):
    rows = [g for g in game_log
            if (g.get("offense") or {}).get("plays") and g.get("opponent")]
    if len(rows) < 4:
        return None
    x = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ov = [g["offense"].get("success_rate") or 0 for g in rows]
    dv = [(g.get("defense_allowed") or {}).get("success_rate") or 0 for g in rows]
    ax.plot(x, ov, marker="o", linewidth=2.2, color=color, zorder=4,
            label="offense success rate")
    ax.plot(x, dv, marker="s", linewidth=2.2, color=alt, zorder=3,
            label="defense success rate allowed")
    for xi, g in zip(x, rows):
        if g.get("result") == "L":
            ax.axvspan(xi - 0.5, xi + 0.5, color=config.CHART_GRID, alpha=0.35,
                       zorder=0)
    ax.set_xticks(x)
    ax.set_xticklabels(
        [f"{'@' if g.get('site') == 'away' else ''}{g['opponent']}"
         f" ({g.get('result') or '?'})" for g in rows],
        rotation=40, ha="right", fontsize=7.5)
    ax.set_ylim(0, max(max(ov), max(dv)) * 1.25)
    ax.set_ylabel("success rate, %")
    _grid(ax)
    ax.legend(loc="upper right", fontsize=8.5, frameon=False)
    fig.suptitle(f"{team_label}: Week-to-Week Evolution (losses shaded)",
                 fontsize=13, fontweight="bold", color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    return _encode(fig)


def build_season_play_charts(breakdown: dict, game_log: list, team_meta: dict,
                             team_label: str) -> list[dict]:
    """Render the four season play-by-play charts. Failures become placeholders."""
    if not CHARTS_AVAILABLE:
        raise ChartsUnavailable(
            f"matplotlib is not importable on this host ({IMPORT_ERROR}). "
            f"Install it with: pip install -r requirements.txt"
        )
    _apply_style()
    color = (_norm_hex(team_meta.get("color")) if _hex_ok(team_meta.get("color"))
             else config.CHART_FALLBACK_HOME)
    alt = (_norm_hex(team_meta.get("alt_color")) if _hex_ok(team_meta.get("alt_color"))
           else config.CHART_FALLBACK_AWAY)
    # Same contrast guard as the team charts: near-identical or too-pale official
    # colors get a readable stand-in for the second series.
    if _hex_ok(team_meta.get("alt_color")) and _luminance(alt) > 0.82:
        alt = config.CHART_FALLBACK_AWAY
    if _distance(color, alt) < 70:
        alt = config.CHART_FALLBACK_AWAY if _distance(color, config.CHART_FALLBACK_AWAY) >= 70 \
            else config.CHART_MUTED

    builders = {
        "season_directions": lambda: chart_season_directions(breakdown, team_label, color, alt),
        "season_downs": lambda: chart_season_downs(breakdown, team_label, color, alt),
        "season_third": lambda: chart_season_third(breakdown, team_label, color, alt),
        "season_trend": lambda: chart_season_trend(game_log, team_label, color, alt),
    }

    out: list[dict] = []
    for key, title, caption in SEASON_PLAY_CHART_SPECS:
        img, available = None, True
        try:
            img = builders[key]()
        except Exception as e:
            logging.warning(f"Season play chart '{key}' failed to render: {e}")
            img = None
        if not img:
            available = False
            img = _placeholder(title, "No data available from CollegeFootballData for this chart.")
        out.append({"key": key, "title": title, "caption": caption,
                    "img": img, "available": available})
    return out


# ---------------------------------------------------------------------------
# Game conditions card — the forecast and the ground it is played on
# ---------------------------------------------------------------------------
def _compass(degrees) -> str:
    try:
        d = float(degrees) % 360
    except (TypeError, ValueError):
        return ""
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int((d + 22.5) // 45) % 8]


def chart_conditions(conditions, accent_c):
    """A drawn card: weather on the left, the venue (with a field sketch) on the right."""
    conditions = conditions or {}
    weather = conditions.get("weather") or {}
    venue = conditions.get("venue") or {}
    has_weather = weather.get("available") is not False and any(
        weather.get(k) is not None for k in
        ("temperature", "windSpeed", "precipitation", "weatherCondition", "gameIndoors"))
    has_venue = bool(venue) and venue.get("available") is not False and venue.get("name")
    if not has_weather and not has_venue:
        return None

    fig, ax = plt.subplots(figsize=(FIG_W, 4.3))
    ax.axis("off")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)

    for x0 in (1, 51):
        ax.add_patch(plt.Rectangle((x0, 2), 48, 96, fill=False,
                                   edgecolor=config.CHART_GRID, linewidth=1.4))

    # ---- left: the weather ------------------------------------------------
    ax.text(25, 90, "CONDITIONS", ha="center", fontsize=10, fontweight="bold",
            color=config.CHART_MUTED)
    if not has_weather:
        ax.text(25, 50, "No conditions data\nstored for this game", ha="center",
                va="center", fontsize=11, color=config.CHART_MUTED)
    elif weather.get("gameIndoors"):
        ax.text(25, 56, "INDOORS", ha="center", fontsize=26, fontweight="bold",
                color=accent_c)
        ax.text(25, 40, "climate controlled — weather is not a factor",
                ha="center", fontsize=9.5, color=config.CHART_TEXT)
    else:
        temp = weather.get("temperature")
        if temp is not None:
            ax.text(17, 60, f"{round(float(temp))}°F", ha="center", fontsize=30,
                    fontweight="bold", color=accent_c)
        condition = weather.get("weatherCondition")
        if condition:
            ax.text(17, 44, str(condition), ha="center", fontsize=11,
                    color=config.CHART_TEXT)
        wind = weather.get("windSpeed")
        if wind is not None:
            direction = _compass(weather.get("windDirection"))
            try:
                angle = math.radians(90 - float(weather.get("windDirection") or 0))
                dx, dy = math.cos(angle) * 7, math.sin(angle) * 7
                ax.annotate("", xy=(37 + dx, 56 + dy), xytext=(37 - dx, 56 - dy),
                            arrowprops=dict(arrowstyle="-|>", linewidth=2.2,
                                            color=config.CHART_TEXT))
            except (TypeError, ValueError):
                pass
            ax.text(37, 42, f"wind {round(float(wind))} mph"
                    f"{' ' + direction if direction else ''}",
                    ha="center", fontsize=9.5, color=config.CHART_TEXT)
        detail = []
        precip = weather.get("precipitation")
        if precip:
            detail.append(f"precipitation {precip} in")
        snow = weather.get("snowfall")
        if snow:
            detail.append(f"snowfall {snow} in")
        humidity = weather.get("humidity")
        if humidity is not None:
            detail.append(f"humidity {round(float(humidity))}%")
        if detail:
            ax.text(25, 26, "  ·  ".join(detail), ha="center", fontsize=9,
                    color=config.CHART_MUTED)
        elif temp is not None:
            ax.text(25, 26, "no precipitation expected", ha="center", fontsize=9,
                    color=config.CHART_MUTED)

    # ---- right: the venue -------------------------------------------------
    ax.text(75, 90, "THE VENUE", ha="center", fontsize=10, fontweight="bold",
            color=config.CHART_MUTED)
    if not has_venue:
        ax.text(75, 50, "Venue details\nunavailable", ha="center", va="center",
                fontsize=11, color=config.CHART_MUTED)
    else:
        name = str(venue.get("name") or "")
        ax.text(75, 80, name if len(name) <= 34 else name[:33] + "…", ha="center",
                fontsize=12.5, fontweight="bold", color=config.CHART_TEXT)
        place = ", ".join(x for x in (venue.get("city"), venue.get("state")) if x)
        facts = []
        if venue.get("capacity"):
            facts.append(f"capacity {int(venue['capacity']):,}")
        if venue.get("elevation_m") is not None:
            facts.append(f"elev. {int(venue['elevation_m']):,} m")
        if venue.get("built"):
            facts.append(f"opened {venue['built']}")
        ax.text(75, 72, place, ha="center", fontsize=9.5, color=config.CHART_MUTED)
        ax.text(75, 64, "  ·  ".join(facts), ha="center", fontsize=9.5,
                color=config.CHART_TEXT)

        # The field sketch: surface color says grass vs turf, an arc says dome.
        grass = (venue.get("surface") or "").startswith("grass")
        field_c = "#3f7d44" if grass else "#4c9c6b"
        fx0, fx1, fy0, fy1 = 57, 93, 12, 42
        ax.add_patch(plt.Rectangle((fx0, fy0), fx1 - fx0, fy1 - fy0,
                                   facecolor=field_c, edgecolor="white",
                                   linewidth=1.5, zorder=3))
        for i in range(1, 6):
            x = fx0 + (fx1 - fx0) * i / 6
            ax.plot([x, x], [fy0 + 1, fy1 - 1], color="white", linewidth=0.9,
                    alpha=0.85, zorder=4)
        ax.text((fx0 + fx1) / 2, (fy0 + fy1) / 2,
                "GRASS" if grass else "TURF", ha="center", va="center",
                fontsize=11, fontweight="bold", color="white", alpha=0.9, zorder=5)
        if (venue.get("stadium_type") or "") == "dome":
            arc = np.linspace(0, math.pi, 60)
            ax.plot(fx0 + (fx1 - fx0) * (1 - np.cos(arc)) / 2,
                    fy1 + np.sin(arc) * 9, color=config.CHART_TEXT,
                    linewidth=2.2, zorder=4)
            ax.text((fx0 + fx1) / 2, fy1 + 13, "DOME", ha="center", fontsize=8.5,
                    fontweight="bold", color=config.CHART_TEXT)
        ax.text((fx0 + fx1) / 2, fy0 - 5,
                f"{venue.get('surface', '')} · {venue.get('stadium_type', '')}",
                ha="center", fontsize=9, color=config.CHART_MUTED)

    fig.suptitle("Game Conditions", fontsize=13, fontweight="bold",
                 color=config.CHART_TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _encode(fig)
