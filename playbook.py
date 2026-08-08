"""Play-by-play analytics, shared by the game recap and the season play report.

Everything here is pure computation over CFBD /plays rows. Two layers of depth:

  play_type_breakdown()    what was run, by play type and rush/dropback family
  situational_breakdown()  the deeper cut — WHERE runs went (gap and side, parsed
                           from the play text), scrambles and screens separated from
                           designed plays, tendencies and success by down, third-down
                           conversions by distance, fourth-down decisions, red zone

The standard down-and-distance success definition anchors every rate, so a number in
one report means the same thing in every other.
"""

import re

SUCCESS_DEFINITION = (
    "A play is a success when it gains 50% of the yards to go on 1st down, 70% on "
    "2nd down, or 100% on 3rd/4th down; offensive touchdowns always count, turnovers "
    "never do. Special-teams and administrative plays are excluded."
)

_NON_SCRIMMAGE = ("kickoff", "punt", "field goal", "timeout", "end period",
                  "end of half", "end of game", "penalty", "extra point",
                  "two point", "defensive 2pt")


def _is_scrimmage(play: dict) -> bool:
    text = (play.get("playType") or "").lower()
    if not text or any(marker in text for marker in _NON_SCRIMMAGE):
        return False
    return play.get("yardsGained") is not None


def _is_turnover(play: dict) -> bool:
    text = (play.get("playType") or "").lower()
    return "interception" in text or "fumble recovery (opponent)" in text \
        or "fumble return" in text


def _is_success(play: dict) -> bool:
    """The offense's side of the standard success-rate definition."""
    if _is_turnover(play):
        return False
    if play.get("scoring") and not _is_turnover(play):
        return True
    yards = int(play.get("yardsGained") or 0)
    down = int(play.get("down") or 0)
    distance = play.get("distance")
    if not distance or int(distance) <= 0:
        return yards > 0
    distance = int(distance)
    if down <= 1:
        return yards >= 0.5 * distance
    if down == 2:
        return yards >= 0.7 * distance
    return yards >= distance


def _family(play_type: str) -> str:
    text = (play_type or "").lower()
    if "rush" in text:
        return "rush"
    if "pass" in text or "sack" in text or "interception" in text:
        return "dropback"
    return "other"


# ---------------------------------------------------------------------------
# Play-text parsing — the direction and detail CFBD's playType does not carry
# ---------------------------------------------------------------------------
RUSH_DIRECTIONS = ("left end", "left tackle", "left guard", "middle",
                   "right guard", "right tackle", "right end")


def rush_direction(play: dict) -> str:
    """Where a rush went, parsed from the play text.

    'scramble' is returned for QB scrambles — they carry a Rush play type but are
    broken plays, not designed runs, and belong with the dropback story.
    'unclassified' is an honest bucket, not a failure: not every play text names
    a gap, and a made-up direction would poison the tendency numbers.
    """
    text = (play.get("playText") or "").lower()
    if "scramble" in text:
        return "scramble"
    if "up the middle" in text or "middle for" in text:
        return "middle"
    for side in ("left", "right"):
        for gap in ("end", "tackle", "guard"):
            if f"{side} {gap}" in text:
                return f"{side} {gap}"
    return "unclassified"


_DEPTH_RE = re.compile(r"\b(deep|short)\b")
_AREA_RE = re.compile(r"\b(?:deep|short)\s+(left|middle|right)\b")


def pass_detail(play: dict) -> dict:
    """What the text says about a dropback: depth, area, screen, scramble, sack."""
    text = (play.get("playText") or "").lower()
    ptype = (play.get("playType") or "").lower()
    out = {"sack": "sack" in ptype or "sacked" in text,
           "scramble": "scramble" in text,
           "screen": "screen" in text,
           "interception": "interception" in ptype or "intercept" in text}
    depth = _DEPTH_RE.search(text)
    out["depth"] = depth.group(1) if depth else None
    area = _AREA_RE.search(text)
    out["area"] = area.group(1) if area else None
    return out


# ---------------------------------------------------------------------------
# Aggregation primitives
# ---------------------------------------------------------------------------
def _agg(plays: list[dict]) -> dict:
    yards = sum(int(p.get("yardsGained") or 0) for p in plays)
    successes = sum(1 for p in plays if _is_success(p))
    return {
        "plays": len(plays),
        "yards": yards,
        "yards_per_play": round(yards / len(plays), 2) if plays else None,
        "success_rate": round(successes / len(plays) * 100, 1) if plays else None,
        "explosive_15plus": sum(1 for p in plays if (p.get("yardsGained") or 0) >= 15),
        "stuffed_zero_or_less": sum(1 for p in plays
                                    if (p.get("yardsGained") or 0) <= 0),
    }


def _type_rows(plays: list[dict]) -> list[dict]:
    groups: dict = {}
    for p in plays:
        groups.setdefault(p.get("playType") or "Unknown", []).append(p)
    rows = [{"type": ptype, **_agg(group)} for ptype, group in groups.items()]
    rows.sort(key=lambda r: -r["plays"])
    return rows


def _family_rows(plays: list[dict]) -> dict:
    out = {}
    for family in ("rush", "dropback"):
        group = [p for p in plays if _family(p.get("playType")) == family]
        if group:
            out[family] = {k: v for k, v in _agg(group).items()
                           if k in ("plays", "yards", "yards_per_play", "success_rate")}
    return out


def play_type_breakdown(plays: list, home: str, away: str) -> dict:
    """Offense and defense play-type effectiveness for both teams of one game."""
    scrimmage = [p for p in plays if _is_scrimmage(p)]

    def offense_of(team):
        return [p for p in scrimmage if p.get("offense") == team]

    out = {"definition": SUCCESS_DEFINITION}
    for team, opponent in ((home, away), (away, home)):
        out[team] = {
            "offense_by_type": _type_rows(offense_of(team)),
            "offense_rush_vs_dropback": _family_rows(offense_of(team)),
            "defense_allowed_by_type": _type_rows(offense_of(opponent)),
            "defense_rush_vs_dropback_allowed": _family_rows(offense_of(opponent)),
        }
    return out


# ---------------------------------------------------------------------------
# The deeper cut: direction, down and distance, situations
# ---------------------------------------------------------------------------
def _converted(play: dict) -> bool:
    if play.get("scoring") and not _is_turnover(play):
        return True
    distance = play.get("distance")
    if not distance or int(distance) <= 0:
        return (play.get("yardsGained") or 0) > 0
    return int(play.get("yardsGained") or 0) >= int(distance)


def _distance_bucket(play: dict) -> str:
    d = int(play.get("distance") or 0)
    if d <= 3:
        return "short_1_3"
    if d <= 6:
        return "medium_4_6"
    return "long_7plus"


def situational_profile(plays: list[dict]) -> dict:
    """The full deep profile of ONE side's scrimmage plays (already filtered)."""
    designed_rushes = [p for p in plays
                       if _family(p.get("playType")) == "rush"
                       and rush_direction(p) != "scramble"]
    dropbacks = [p for p in plays if _family(p.get("playType")) == "dropback"]
    scrambles = [p for p in plays if rush_direction(p) == "scramble"]

    # WHERE the ground game went. Unclassified stays visible so a thin play-text
    # feed reads as "direction unknown", never as a fabricated tendency.
    directions = {}
    for direction in (*RUSH_DIRECTIONS, "unclassified"):
        group = [p for p in designed_rushes if rush_direction(p) == direction]
        if group:
            directions[direction] = _agg(group)

    details = [pass_detail(p) for p in dropbacks]
    passing = {
        "dropbacks": _agg(dropbacks),
        "sacks": sum(1 for d in details if d["sack"]),
        "screens": _agg([p for p, d in zip(dropbacks, details) if d["screen"]]),
        "interceptions": sum(1 for d in details if d["interception"]),
        "throws_by_depth": {
            depth: _agg([p for p, d in zip(dropbacks, details) if d["depth"] == depth])
            for depth in ("short", "deep")
            if any(d["depth"] == depth for d in details)
        },
        "depth_unknown": sum(1 for d in details if d["depth"] is None
                             and not d["sack"] and not d["scramble"]),
    }

    by_down = {}
    for down in (1, 2, 3, 4):
        group = [p for p in plays if int(p.get("down") or 0) == down]
        if not group:
            continue
        rushes = sum(1 for p in group if _family(p.get("playType")) == "rush")
        by_down[str(down)] = {
            **_agg(group),
            "rush_share_pct": round(rushes / len(group) * 100, 1),
        }

    third = [p for p in plays if int(p.get("down") or 0) == 3]
    third_down = {}
    for bucket in ("short_1_3", "medium_4_6", "long_7plus"):
        group = [p for p in third if _distance_bucket(p) == bucket]
        if not group:
            continue
        third_down[bucket] = {
            "attempts": len(group),
            "conversions": sum(1 for p in group if _converted(p)),
            "conversion_pct": round(
                sum(1 for p in group if _converted(p)) / len(group) * 100, 1),
            "rush_share_pct": round(
                sum(1 for p in group if _family(p.get("playType")) == "rush")
                / len(group) * 100, 1),
        }

    fourth = [p for p in plays if int(p.get("down") or 0) == 4]
    red_zone = [p for p in plays
                if p.get("yardsToGoal") is not None
                and int(p.get("yardsToGoal") or 100) <= 20]

    return {
        "rush_directions": directions,
        "scrambles": _agg(scrambles) if scrambles else {"plays": 0},
        "passing": passing,
        "by_down": by_down,
        "third_down_by_distance": third_down,
        "fourth_down_gone_for": {
            "attempts": len(fourth),
            "conversions": sum(1 for p in fourth if _converted(p)),
        },
        "red_zone": {
            **_agg(red_zone),
            "touchdowns": sum(1 for p in red_zone
                              if p.get("scoring") and not _is_turnover(p)),
        } if red_zone else {"plays": 0},
    }


def team_breakdown(plays: list, team: str) -> dict:
    """One team's view of any slice of plays — a game, a month, a whole season.

    Both layers of depth for both sides of the ball: the play-type/family breakdown
    and the full situational profile, for the team's offense and for what its
    defense allowed.
    """
    scrimmage = [p for p in plays if _is_scrimmage(p)]
    offense = [p for p in scrimmage if p.get("offense") == team]
    allowed = [p for p in scrimmage
               if p.get("defense") == team and p.get("offense") != team]

    def side(rows):
        return {
            "by_type": _type_rows(rows),
            "rush_vs_dropback": _family_rows(rows),
            "situational": situational_profile(rows),
        }

    return {
        "definition": SUCCESS_DEFINITION,
        "note": ("Directions are parsed from the play text; 'unclassified' means the "
                 "text named no gap. Scrambles are broken out from designed rushes. "
                 "'defense_allowed' is everything opposing offenses did against this "
                 "team."),
        "offense": side(offense),
        "defense_allowed": side(allowed),
    }


def situational_breakdown(plays: list, home: str, away: str) -> dict:
    """Both teams of one game, offense and defense views of the deep profile."""
    scrimmage = [p for p in plays if _is_scrimmage(p)]
    out = {"definition": SUCCESS_DEFINITION,
           "note": ("Directions are parsed from the play text; 'unclassified' means "
                    "the text named no gap. Scrambles are broken out from designed "
                    "rushes. The defense view is what that team's defense ALLOWED.")}
    for team, opponent in ((home, away), (away, home)):
        out[team] = {
            "offense": situational_profile(
                [p for p in scrimmage if p.get("offense") == team]),
            "defense_allowed": situational_profile(
                [p for p in scrimmage if p.get("offense") == opponent]),
        }
    return out
