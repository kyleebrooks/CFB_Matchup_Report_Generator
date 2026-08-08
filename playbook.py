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
def _ppa_values(plays: list[dict]) -> list[float]:
    out = []
    for p in plays:
        try:
            if p.get("ppa") is not None:
                out.append(float(p["ppa"]))
        except (TypeError, ValueError):
            continue
    return out


def _agg(plays: list[dict]) -> dict:
    yards = sum(int(p.get("yardsGained") or 0) for p in plays)
    successes = sum(1 for p in plays if _is_success(p))
    # CFBD scores every play with PPA (predicted points added) — the per-play value
    # measure that separates "5 yards on 3rd-and-4" from "5 yards on 3rd-and-12".
    ppa = _ppa_values(plays)
    return {
        "plays": len(plays),
        "yards": yards,
        "yards_per_play": round(yards / len(plays), 2) if plays else None,
        "success_rate": round(successes / len(plays) * 100, 1) if plays else None,
        "avg_ppa": round(sum(ppa) / len(ppa), 3) if ppa else None,
        "explosive_15plus": sum(1 for p in plays if (p.get("yardsGained") or 0) >= 15),
        "stuffed_zero_or_less": sum(1 for p in plays
                                    if (p.get("yardsGained") or 0) <= 0),
    }


def play_group(play: dict) -> str:
    """The display group a play belongs to.

    CFBD's raw playType splits what a reader thinks of as one thing: receptions,
    incompletions, passing TDs and interceptions are all outcomes of the same call
    (grading 'Pass Incompletion' as its own play type is meaningless), and rushing
    TDs are just rushes that scored. Sacks stay their own group — they are the
    drive-killers a reader wants counted separately.
    """
    pt = (play.get("playType") or "").lower()
    family = _family(pt)
    if family == "rush":
        return "Rushes"
    if family == "dropback":
        return "Sacks" if "sack" in pt else "Passes"
    return "Other"


def _group_rows(plays: list[dict]) -> list[dict]:
    buckets: dict = {}
    for p in plays:
        buckets.setdefault(play_group(p), []).append(p)

    def low(p):
        return (p.get("playType") or "").lower()

    rows = []
    for name, group in buckets.items():
        row = {"group": name, **_agg(group)}
        if name == "Passes":
            interceptions = sum(1 for p in group if "interception" in low(p))
            incompletions = sum(1 for p in group if "incompletion" in low(p))
            completions = len(group) - interceptions - incompletions
            row.update({
                "attempts": len(group),
                "completions": completions,
                "incompletions": incompletions,
                "interceptions_thrown": interceptions,
                "completion_pct": round(completions / len(group) * 100, 1),
                "touchdowns": sum(1 for p in group if "touchdown" in low(p)),
            })
        elif name == "Rushes":
            row.update({
                "touchdowns": sum(1 for p in group if "touchdown" in low(p)),
                "scrambles": sum(1 for p in group if rush_direction(p) == "scramble"),
            })
        rows.append(row)
    rows.sort(key=lambda r: -r["plays"])
    return rows


def _family_rows(plays: list[dict]) -> dict:
    out = {}
    for family in ("rush", "dropback"):
        group = [p for p in plays if _family(p.get("playType")) == family]
        if group:
            out[family] = {k: v for k, v in _agg(group).items()
                           if k in ("plays", "yards", "yards_per_play",
                                    "success_rate", "avg_ppa")}
    return out


def play_type_breakdown(plays: list, home: str, away: str) -> dict:
    """Offense and defense play-group effectiveness for both teams of one game."""
    scrimmage = [p for p in plays if _is_scrimmage(p)]

    def offense_of(team):
        return [p for p in scrimmage if p.get("offense") == team]

    out = {
        "definition": SUCCESS_DEFINITION,
        "note": ("Plays are grouped the way a reader calls them: Passes covers every "
                 "pass attempt (completions, incompletions, passing TDs, "
                 "interceptions — with the completion detail inside the row), Rushes "
                 "includes rushing TDs and notes scrambles, and Sacks stand alone."),
    }
    for team, opponent in ((home, away), (away, home)):
        out[team] = {
            "offense_play_groups": _group_rows(offense_of(team)),
            "offense_rush_vs_dropback": _family_rows(offense_of(team)),
            "defense_allowed_play_groups": _group_rows(offense_of(opponent)),
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

    # Modern ESPN play text usually names no gap at all, so direction data is only
    # as good as its coverage — report that plainly, and consumers below a sane
    # threshold should skip direction claims rather than analyse noise.
    classified = sum(v["plays"] for k, v in directions.items() if k != "unclassified")
    direction_coverage = {
        "designed_rushes": len(designed_rushes),
        "classified": classified,
        "classified_pct": round(classified / len(designed_rushes) * 100, 1)
                          if designed_rushes else None,
        "note": ("Directions come from parsing the play text, which often names no "
                 "gap. Below ~25% coverage, direction tendencies are noise — skip "
                 "them and analyse the ground game by outcome instead."),
    }

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

    # The full negative-play ledger — sacks are only one way a snap goes backwards.
    negative = [p for p in plays if int(p.get("yardsGained") or 0) < 0]
    low = lambda p: (p.get("playType") or "").lower()
    sacks_taken = sum(1 for p in negative if "sack" in low(p))
    rushes_for_loss = sum(1 for p in negative if _family(p.get("playType")) == "rush")
    negative_plays = {
        "plays": len(negative),
        "rate_pct": round(len(negative) / len(plays) * 100, 1) if plays else None,
        "yards_lost": sum(int(p.get("yardsGained") or 0) for p in negative),
        "sacks": sacks_taken,
        "rushes_for_loss": rushes_for_loss,
        "other_for_loss": len(negative) - sacks_taken - rushes_for_loss,
        "turnovers": {
            "interceptions": sum(1 for p in plays if "interception" in low(p)),
            "fumbles_lost": sum(1 for p in plays
                                if _is_turnover(p) and "fumble" in low(p)),
        },
        "note": ("A negative play is any scrimmage snap that lost yards; turnovers "
                 "are counted whatever the yardage said."),
    }

    # The outcome distribution needs no play text at all, so it is always
    # reportable — the honest fallback when direction coverage is thin.
    outcome_bands = (("loss", lambda y: y < 0), ("no_gain", lambda y: y == 0),
                     ("short_1_3", lambda y: 1 <= y <= 3),
                     ("solid_4_9", lambda y: 4 <= y <= 9),
                     ("chunk_10_14", lambda y: 10 <= y <= 14),
                     ("breakaway_15plus", lambda y: y >= 15))
    rush_outcomes = {
        band: sum(1 for p in designed_rushes
                  if test(int(p.get("yardsGained") or 0)))
        for band, test in outcome_bands
    }

    return {
        "rush_directions": directions,
        "rush_direction_coverage": direction_coverage,
        "designed_rush_outcomes": rush_outcomes,
        "negative_plays": negative_plays,
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
            "play_groups": _group_rows(rows),
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


# ---------------------------------------------------------------------------
# Play-calling grades and unit rankings
# ---------------------------------------------------------------------------
GRADE_LADDER = [(97, "A+"), (93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"),
                (77, "C+"), (73, "C"), (70, "C-"), (67, "D+"), (63, "D"), (60, "D-"),
                (55, "F+"), (45, "F"), (0, "F-")]


def _letter(score: float) -> str:
    for cutoff, grade in GRADE_LADDER:
        if score >= cutoff:
            return grade
    return "F-"


def playcalling_report(plays: list, team: str) -> dict:
    """Grade one team's play-calling from its own offensive play-by-play.

    Deterministic on purpose: the same plays always earn the same grade, so a grade
    means the same thing in every report. Each component is scaled against
    FBS-typical ranges (the floor scores 0, the ceiling 100), weighted, and the
    composite maps onto the school ladder from F- to A+.
    """
    offense = [p for p in plays if _is_scrimmage(p) and p.get("offense") == team]
    if len(offense) < 20:
        return {"available": False,
                "note": f"Fewer than 20 scrimmage plays for {team} — not enough to "
                        f"grade the play-calling."}

    def scaled(value, floor, ceiling):
        return round(max(0.0, min(100.0, (value - floor) / (ceiling - floor) * 100)), 1)

    first = [p for p in offense if int(p.get("down") or 0) == 1]
    third = [p for p in offense if int(p.get("down") or 0) == 3]
    fourth = [p for p in offense if int(p.get("down") or 0) == 4]

    components: dict = {}

    def component(key, value, floor, ceiling, weight, what, invert=False):
        if value is None:
            return
        score = scaled(value, floor, ceiling)
        if invert:
            score = round(100 - score, 1)
        components[key] = {"value_pct": round(value * 100, 1), "score": score,
                           "weight": weight, "what": what}

    if first:
        component("early_down_success",
                  sum(1 for p in first if _is_success(p)) / len(first), 0.30, 0.60, 30,
                  "1st-down success rate — staying ahead of schedule is play-calling's "
                  "first job")
    if third:
        component("schedule_management",
                  sum(1 for p in third if int(p.get("distance") or 0) <= 6) / len(third),
                  0.30, 0.75, 15,
                  "share of 3rd downs kept to 6 yards or less — the calls on 1st and "
                  "2nd decide this")
        component("third_down_conversion",
                  sum(1 for p in third if _converted(p)) / len(third), 0.25, 0.55, 15,
                  "3rd downs actually converted")
    component("explosive_play_creation",
              sum(1 for p in offense if (p.get("yardsGained") or 0) >= 15) / len(offense),
              0.04, 0.16, 15, "share of snaps gaining 15+ — scheme creating chunk plays")
    component("negative_play_avoidance",
              sum(1 for p in offense
                  if int(p.get("yardsGained") or 0) < 0 or _is_turnover(p)) / len(offense),
              0.06, 0.22, 15,
              "share of snaps losing yards or the ball (lower is better)", invert=True)

    red_zone = [p for p in offense
                if p.get("yardsToGoal") is not None
                and int(p.get("yardsToGoal") or 100) <= 20]
    rz_tds = sum(1 for p in red_zone if p.get("scoring") and not _is_turnover(p))
    rz_trips = {p.get("driveId") for p in red_zone if p.get("driveId") is not None}
    if rz_trips:
        component("red_zone_finishing", rz_tds / len(rz_trips), 0.30, 0.80, 10,
                  "touchdowns per red-zone trip")
    elif red_zone:
        component("red_zone_finishing",
                  sum(1 for p in red_zone if _is_success(p)) / len(red_zone),
                  0.35, 0.65, 10, "red-zone success rate (drive ids unavailable)")

    total_weight = sum(c["weight"] for c in components.values())
    composite = (sum(c["score"] * c["weight"] for c in components.values()) / total_weight
                 if total_weight else 50.0)

    # Fourth down is a decision quality signal, not a volume stat: reward staffs
    # whose gambles convert, dock the ones that keep failing.
    adjustment = 0.0
    if fourth:
        conv = sum(1 for p in fourth if _converted(p)) / len(fourth)
        adjustment = 3.0 if conv >= 0.5 else (-3.0 if len(fourth) >= 2 else -1.5)
    composite = max(0.0, min(100.0, composite + adjustment))

    return {
        "available": True,
        "team": team,
        "grade": _letter(composite),
        "score": round(composite, 1),
        "plays_graded": len(offense),
        "components": components,
        "fourth_down_adjustment": {
            "attempts": len(fourth),
            "conversions": sum(1 for p in fourth if _converted(p)),
            "applied": adjustment,
        },
        "rubric": ("Computed deterministically from the play-by-play: each component "
                   "is scaled against FBS-typical ranges and weighted as shown, with "
                   "a fourth-down decision adjustment. Present THIS grade — explain "
                   "it, never re-derive or soften it."),
    }


def unit_report(plays: list, home: str, away: str) -> dict:
    """All eight units of a game (or season slice), ranked best to worst.

    Offenses are scored by the per-play value they created, defenses by the value
    they prevented — so a shutdown pass defense can outrank a good rushing offense
    and the ranking reads as one honest list.
    """
    scrimmage = [p for p in plays if _is_scrimmage(p)]
    entries = []
    for team, opponent in ((home, away), (away, home)):
        for side, source, families in (
                ("offense", team, (("rush", "rushing offense"),
                                   ("dropback", "passing offense"))),
                ("defense", opponent, (("rush", "run defense"),
                                       ("dropback", "pass defense")))):
            rows_all = [p for p in scrimmage if p.get("offense") == source]
            for family, label in families:
                rows = [p for p in rows_all if _family(p.get("playType")) == family]
                if len(rows) < 5:
                    continue
                agg = _agg(rows)
                base = (agg["avg_ppa"] if agg["avg_ppa"] is not None
                        else ((agg["success_rate"] or 0) / 100) - 0.42)
                score = base if side == "offense" else -base
                entries.append({
                    "team": team,
                    "unit": label,
                    "perspective": "created" if side == "offense" else "allowed",
                    "plays": agg["plays"],
                    "yards_per_play": agg["yards_per_play"],
                    "success_rate": agg["success_rate"],
                    "avg_ppa": agg["avg_ppa"],
                    "score": round(score, 3),
                })
    entries.sort(key=lambda e: -e["score"])
    return {
        "note": ("All units ranked best to worst on per-play value: offenses by the "
                 "PPA they created, defenses by the PPA they prevented. 'allowed' "
                 "rows show what opposing offenses did against that unit."),
        "ranking": entries,
    }
