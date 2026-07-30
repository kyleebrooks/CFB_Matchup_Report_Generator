"""Stage 2: the single Kimi K3 synthesis call that writes the finished report.

No web search here — every fact this model is allowed to use has already been gathered:
CFBD statistics, the eight research buckets, the Rotowire feed, and the computed
statistical baseline. Its job is to reason over that bundle and produce the prose.
"""

import json
import logging

import config
import openrouter

SYSTEM_PROMPT = (
    "You are a top-tier, seasoned sports analyst, handicapper and writer covering college "
    "football. You write with the confident, authoritative voice of a national analyst and a "
    "streak of dark humor where it fits. You avoid cheesy, overused sportswriting cliches. "
    "You work strictly from the data you are given — you do not invent statistics, players, "
    "quotes, sources, or news. Your predictions are graded against the actual final score, so "
    "precision matters more than hedging."
)

# Section order is fixed so every report reads the same way.
def _section_plan(home_full: str, away_full: str) -> list[tuple[str, str]]:
    return [
        ("Matchup Overview",
         "Set the stage: who these teams are, what is at stake, relevant history and context "
         "for this specific meeting. This is the only place for scene-setting — there must be "
         "no introduction before it and no closing remarks after the final section."),
        ("SP Ratings",
         "Use the SP+ overall, offense, defense and special teams RATING values. Ignore the "
         "'ranking' field entirely — it is always 1 in this feed and is meaningless here. Note "
         "that a LOWER SP+ defensive rating is better."),
        ("ELO Ratings", "Elo is a head-to-head power rating; explain what the gap implies."),
        ("FPI Ratings", "FPI is points-above-average against an average opponent."),
        ("Advanced Team Stats",
         "Success rate, explosiveness, PPA per play, line yards, havoc, points per scoring "
         "opportunity. Use the supplied national percentiles to give each number context."),
        ("Returning Production", "How much of last season's production each team returned."),
        ("Team Talent", "The 247 composite talent score — the recruiting base underneath the roster."),
        ("Team PPA", "Team-level predicted points added, including the week-by-week trend."),
        ("Player PPA", "The individuals driving each team's production."),
        ("Team Season Stats", "The raw counting stats, and where they agree or disagree with the advanced numbers."),
        ("Adjusted Team Metrics", "Opponent-adjusted efficiency."),
        (f"{home_full} Injury Updates",
         "MEDICAL items only. This section has TWO separate inputs — live web research and the "
         "Rotowire feed — and you must present them as clearly labeled separate groups."),
        (f"{away_full} Injury Updates",
         "MEDICAL items only. Same two-input structure as above; keep the groups separate."),
        ("Key Player Matchups",
         "The defining individual battles — a specific WR against the CB who will travel with "
         "him, a RB against a particular front seven, an OL against a named edge rusher. Tie "
         "each one back to the Player PPA and advanced numbers where you can."),
        (f"{home_full} Roster Updates", "NON-INJURY roster news only."),
        (f"{away_full} Roster Updates", "NON-INJURY roster news only."),
        (f"{home_full} Practice and Scrimmage Updates", "Practice participation, scrimmage results, camp reporting."),
        (f"{away_full} Practice and Scrimmage Updates", "Practice participation, scrimmage results, camp reporting."),
        (f"{home_full} vs {away_full} Media Matchup Analysis",
         "What the national and local media, analysts and coaches are saying about this game."),
        ("Final Prediction",
         "Your overall verdict, a projected final score, and YOUR point spread. Weigh every "
         "input above: the ratings, the efficiency mismatches, the form trend, the injuries, the "
         "roster and practice news, and the statistical baseline."),
    ]


def _format_sections(home_full: str, away_full: str) -> str:
    lines = []
    for i, (title, guidance) in enumerate(_section_plan(home_full, away_full), start=1):
        lines.append(f"{i}. {title} — {guidance}")
    return "\n".join(lines)


def _chart_manifest(charts: list[dict]) -> str:
    lines = []
    for c in charts:
        state = "rendered" if c.get("available") else "NO DATA — placeholder shown, do not describe it"
        lines.append(f'- "{c["title"]}" ({state}): {c["caption"]}')
    return "\n".join(lines)


def _sources_manifest(registry) -> str:
    lines = []
    for entry in registry.entries():
        label = entry["title"] or entry["publisher"] or entry["url"]
        lines.append(f'[{entry["index"]}] {label} — {entry["url"]}')
    return "\n".join(lines)


def build_prompt(ctx: dict, bundle: dict, charts: list[dict], registry) -> str:
    home_full, away_full = ctx["home_full"], ctx["away_full"]
    now = ctx["now_utc"]

    return f"""Write a full-length matchup report for {home_full} (home) vs {away_full} (away), {ctx['year']} season.

Report generated: {now.strftime('%A, %B %d, %Y at %H:%M UTC')}.

=========================== SECTIONS ===========================
Produce EXACTLY these sections, in this order, and nothing else. No preamble before the
first section, no sign-off after the last.

{_format_sections(home_full, away_full)}

FORMATTING
- Each section heading is a level-2 markdown heading on its own line: "## Section Title".
  Do not number them and do not add any other heading levels above them.
- Open each section with one short line explaining what the section covers, and — when it is
  built on a statistic — what that statistic actually measures in plain English.
- Then list the key figures (where applicable), then AT LEAST TWO substantial paragraphs of
  analysis explaining how those numbers shape THIS game. Analysis, not restatement.
- When a section has more than one input bucket, keep them visibly separate with a bolded
  sub-label (e.g. "**Live web research:**" and "**Rotowire feed:**") before each group. Never
  blend two sources into one undifferentiated list.
- If a section's data is empty or marked no_data, keep the heading and write ONE sentence
  saying no data was available for that section. Write nothing else in it — no speculation,
  no filler, no apology.

=========================== SOURCING ===========================
Citation markers are PRE-ASSIGNED. The numbered list below maps each marker to a source that
was actually retrieved. Rules:
- Cite with the exact bracketed marker from that list, e.g. [3], placed inline right after the
  claim it supports. Every news, injury, roster, practice and media claim needs one.
- Use [1] for anything drawn from the CollegeFootballData statistics and [2] for anything from
  the Rotowire feed.
- NEVER invent a marker number that is not on the list, and never write a bare URL.
- Do NOT write a SOURCES or References section — one is appended automatically. Do not mention
  URLs, retrieval problems, search coverage, or which pages you could or could not reach.
- Do not use any fact that is not present in the data below. If it is not here, it does not
  exist for the purposes of this report.

PRE-ASSIGNED CITATION MARKERS:
{_sources_manifest(registry)}

=========================== VISUALS ===========================
These charts are rendered into the finished PDF automatically. Reference them by name in the
relevant sections ("as the Mismatch Matrix shows...") and let your analysis agree with them.
Do not attempt to draw, describe or reproduce them, and never reference a chart marked NO DATA.

{_chart_manifest(charts)}

=========================== PREDICTION ===========================
The "statistical_baseline" block below is a deterministic projection computed from the
ratings — and, where available, the market line — before you saw any of the news. Treat it as
your anchor, not as gospel:
- State the baseline consensus margin explicitly in the Final Prediction section.
- Then adjust it for what the ratings cannot see: injuries to high-PPA players, roster and
  practice developments, matchup-specific edges, and form trend.
- Justify the size of your adjustment. Moving several points off the baseline requires a
  concrete, cited reason. Absent one, stay close to it.
- Close with your projected final score for both teams and your point spread, stated plainly.
  This is YOUR number and it is graded against the real result — be accurate, not diplomatic.

=========================== DATA ===========================
{json.dumps(bundle, ensure_ascii=False, default=str)}
"""


def generate(api_key: str, ctx: dict, bundle: dict, charts: list[dict], registry) -> dict:
    """Call the report model once and return {text, usage, model}."""
    prompt = build_prompt(ctx, bundle, charts, registry)
    logging.info(
        f"Report prompt built: {len(prompt)} chars, "
        f"{len(registry)} sources, model={config.OPENROUTER_REPORT_MODEL}"
    )

    resp = openrouter.chat(
        api_key,
        config.OPENROUTER_REPORT_MODEL,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        effort=config.REPORT_EFFORT,
        max_tokens=config.REPORT_MAX_TOKENS,
        timeout=config.REPORT_TIMEOUT,
    )

    text = openrouter.extract_text(resp)
    if not text:
        raise openrouter.OpenRouterError(
            "Report model returned no text",
            body=json.dumps(resp)[:800],
        )
    return {
        "text": text,
        "usage": openrouter.extract_usage(resp),
        "model": resp.get("model") or config.OPENROUTER_REPORT_MODEL,
        "prompt_chars": len(prompt),
    }
