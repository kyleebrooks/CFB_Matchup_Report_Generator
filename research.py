"""Stage 1: eight parallel GPT-5.6 Luna research calls against the live web.

Each report section that depends on news — not statistics — gets its own dedicated call
with its own recency window and its own citation set. CFBD statistics are never touched
here; this module only gathers reporting.

Results come back as structured buckets so a section with more than one input (injuries
have both a live-web bucket and a Rotowire bucket) keeps its inputs separated and
individually sourced all the way through to the final report.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import config
import openrouter

# ---------------------------------------------------------------------------
# The eight research jobs
# ---------------------------------------------------------------------------
# scope "home"/"away" -> one call per team; "matchup" -> a single call for the game.
RESEARCH_JOBS = [
    {
        "key": "home_injuries",
        "scope": "home",
        "topic": "injury",
        "section": "{home_full} Injury Updates",
        "window": config.INJURY_WINDOW_DAYS,
        "focus": (
            "injuries, injury designations, availability rulings, surgeries, return timelines, "
            "medical redshirts, and week-to-week statuses for CURRENT players on {team_full}"
        ),
        "exclude": "suspensions, transfers, depth-chart moves, and any non-medical roster news",
    },
    {
        "key": "away_injuries",
        "scope": "away",
        "topic": "injury",
        "section": "{away_full} Injury Updates",
        "window": config.INJURY_WINDOW_DAYS,
        "focus": (
            "injuries, injury designations, availability rulings, surgeries, return timelines, "
            "medical redshirts, and week-to-week statuses for CURRENT players on {team_full}"
        ),
        "exclude": "suspensions, transfers, depth-chart moves, and any non-medical roster news",
    },
    {
        "key": "key_player_matchups",
        "scope": "matchup",
        "topic": "key player matchups",
        "section": "Key Player Matchups",
        "window": config.MEDIA_WINDOW_DAYS,
        "focus": (
            "the specific player-vs-player matchups that will decide {home_full} vs {away_full} — "
            "e.g. a WR against the CB expected to shadow him, a RB against a specific front seven, "
            "an OL against a particular edge rusher. Find what beat writers, coaches and analysts "
            "are actually saying about these individual battles for THIS game"
        ),
        "exclude": "generic team-level previews with no named players on both sides",
    },
    {
        "key": "home_roster",
        "scope": "home",
        "topic": "roster",
        "section": "{home_full} Roster Updates",
        "window": config.ROSTER_WINDOW_DAYS,
        "focus": (
            "NON-INJURY roster news for {team_full}: depth-chart changes, starter announcements, "
            "quarterback decisions, suspensions, disciplinary actions, transfer portal entries and "
            "arrivals, eligibility rulings, position switches, and personnel/coaching changes that "
            "affect who plays"
        ),
        "exclude": "anything injury or medical related (that belongs in the injury section)",
    },
    {
        "key": "away_roster",
        "scope": "away",
        "topic": "roster",
        "section": "{away_full} Roster Updates",
        "window": config.ROSTER_WINDOW_DAYS,
        "focus": (
            "NON-INJURY roster news for {team_full}: depth-chart changes, starter announcements, "
            "quarterback decisions, suspensions, disciplinary actions, transfer portal entries and "
            "arrivals, eligibility rulings, position switches, and personnel/coaching changes that "
            "affect who plays"
        ),
        "exclude": "anything injury or medical related (that belongs in the injury section)",
    },
    {
        "key": "home_practice",
        "scope": "home",
        "topic": "practice",
        "section": "{home_full} Practice and Scrimmage Updates",
        "window": config.PRACTICE_WINDOW_DAYS,
        "focus": (
            "practice reports, scrimmage results, spring/fall camp observations, players held out or "
            "limited in practice, install and game-plan notes, and direct coach or player quotes about "
            "how {team_full} has been preparing"
        ),
        "exclude": "game recaps and pure injury reporting without a practice-participation angle",
    },
    {
        "key": "away_practice",
        "scope": "away",
        "topic": "practice",
        "section": "{away_full} Practice and Scrimmage Updates",
        "window": config.PRACTICE_WINDOW_DAYS,
        "focus": (
            "practice reports, scrimmage results, spring/fall camp observations, players held out or "
            "limited in practice, install and game-plan notes, and direct coach or player quotes about "
            "how {team_full} has been preparing"
        ),
        "exclude": "game recaps and pure injury reporting without a practice-participation angle",
    },
    {
        "key": "media_analysis",
        "scope": "matchup",
        "topic": "media analysis",
        "section": "{home_full} vs {away_full} Media Matchup Analysis",
        "window": config.MEDIA_WINDOW_DAYS,
        "focus": (
            "expert previews, analyst predictions, picks against the spread, betting line movement, "
            "national and local media takes, and coaching-staff comments specifically about the "
            "upcoming {home_full} vs {away_full} game"
        ),
        "exclude": "coverage of either team's other opponents or unrelated games",
    },
]

RESEARCH_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "matchup_research",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "as_of_utc": {"type": "string"},
                "no_data": {"type": "boolean"},
                "notes": {"type": "string"},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "headline": {"type": "string"},
                            "detail": {"type": "string"},
                            "player": {"type": "string"},
                            "position": {"type": "string"},
                            "team": {"type": "string"},
                            "status": {"type": "string"},
                            "impact": {"type": "string"},
                            "published": {"type": "string"},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            "source_name": {"type": "string"},
                            "source_url": {"type": "string"},
                        },
                        "required": ["headline", "detail", "published", "source_name", "source_url"],
                    },
                },
            },
            "required": ["findings", "no_data"],
        },
    },
}

SYSTEM_PROMPT = (
    "You are a college football news researcher. You have a live web search tool and you MUST use it "
    "before answering. Your job is to surface the freshest, most specific, verifiable reporting on a "
    "single narrow topic — not to write analysis or prose. Accuracy and recency beat volume every "
    "time. You never guess, never infer from memory, and never report anything you did not read on a "
    "real page you found in search. Return JSON only."
)


def _build_prompt(job: dict, ctx: dict) -> str:
    now = ctx["now_utc"]
    team_full = ctx["home_full"] if job["scope"] == "home" else ctx["away_full"]
    team_short = ctx["home_short"] if job["scope"] == "home" else ctx["away_short"]
    fmt = {
        "home_full": ctx["home_full"],
        "away_full": ctx["away_full"],
        "team_full": team_full,
        "team_short": team_short,
    }
    focus = job["focus"].format(**fmt)
    section = job["section"].format(**fmt)
    if job["scope"] == "matchup":
        subject = (f'the upcoming matchup "{ctx["home_full"]}" (home) vs '
                   f'"{ctx["away_full"]}" (away)')
    elif job["scope"] == "conference":
        subject = f'the {ctx["home_full"]} college football conference'
    elif job["scope"] == "league":
        subject = ctx["home_full"]
    else:
        subject = f'the "{team_full}" college football team'

    kickoff_line = f"Scheduled kickoff: {ctx['kickoff']}.\n" if ctx.get("kickoff") else ""
    if ctx.get("away_full"):
        header = f"MATCHUP: {ctx['home_full']} (home) vs {ctx['away_full']} (away)."
    elif job["scope"] == "league":
        header = f"SUBJECT: {ctx['home_full']}, league-wide."
    else:
        header = f"SUBJECT: the {ctx['home_full']} conference."

    return f"""CURRENT DATE AND TIME: {now.strftime('%A, %B %d, %Y at %H:%M UTC')}
SEASON: {ctx['year']} college football season.
{header}
{kickoff_line}
TASK: Research {subject} and report ONLY on: {focus}.

This feeds the "{section}" section of a matchup report for this exact game.

SEARCH REQUIREMENTS — do this before you answer:
1. Run MULTIPLE web searches. Start broad, then run follow-up searches on every specific
   player name, injury, or storyline you turn up. Do not stop after one search.
2. Hunt for BREAKING, UP-TO-THE-MINUTE news. Explicitly search for today's and yesterday's
   reporting. Sort your effort toward the newest items — a report filed hours ago outranks
   one filed last week. Include beat writers, team sites, local outlets, and credible
   insider accounts, not just national wires.
3. OPEN AND READ the most relevant results. Base every finding on the article's actual
   content, never on a search-result snippet or on your own prior knowledge.
4. Cross-check anything surprising against a second source before you report it.

HARD CONSTRAINTS:
- RECENCY: only items published within the last {job['window']} days, as of the date above.
  Discard anything older, and discard anything you cannot date.
- RELEVANCE: only items that bear on {ctx['home_full']} vs {ctx['away_full']}. Nothing about
  either team's other opponents, other games, or unrelated programs.
- CURRENT PLAYERS ONLY: no former players, alumni, recruits who have not enrolled, or
  players who have already left the program.
- SCOPE: exclude {job['exclude']}.
- NO FABRICATION: every finding needs a real URL you actually retrieved. If you cannot
  source it, leave it out. An empty result is correct and useful; an invented one is not.
- If nothing qualifies, return {{"findings": [], "no_data": true}} and say why in "notes".

OUTPUT — return a single JSON object, no prose outside it, no markdown fences:
{{
  "as_of_utc": "{now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
  "no_data": false,
  "notes": "brief coverage note: what you searched, and any gaps",
  "findings": [
    {{
      "headline": "short factual headline",
      "detail": "2-4 sentences of specifics drawn from the article itself",
      "player": "player name, or empty string if not player-specific",
      "position": "position, or empty string",
      "team": "which of the two teams this concerns",
      "status": "e.g. Out, Questionable, Probable, Season-ending, Starter named, Suspended — or empty",
      "impact": "one sentence on how this changes THIS game",
      "published": "YYYY-MM-DD publication date of the article",
      "confidence": "high | medium | low",
      "source_name": "outlet name",
      "source_url": "full https URL you actually read"
    }}
  ]
}}

Order findings most-recent and most-impactful first. Cap at 12 findings; quality over quantity."""


# ---------------------------------------------------------------------------
# Source registry — deterministic [n] citation numbering
# ---------------------------------------------------------------------------
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "src", "amp",
}


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit((url or "").strip())
        if not parts.scheme or not parts.netloc:
            return (url or "").strip().lower()
        host = parts.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS])
        path = parts.path.rstrip("/") or "/"
        return urlunsplit(("https", host, path, query, ""))
    except Exception:
        return (url or "").strip().lower()


class SourceRegistry:
    """Assigns each distinct URL a stable [n] index.

    The report model is handed these markers and told to reuse them verbatim; the SOURCES
    list itself is rendered by our code, so a citation can never point at a URL the model
    invented or dropped.
    """

    def __init__(self):
        self._index_by_url: dict[str, int] = {}
        self._entries: list[dict] = []

    def add(self, url: str, title: str = "", publisher: str = "") -> int | None:
        if not url or not str(url).strip():
            return None
        key = normalize_url(url)
        if key in self._index_by_url:
            existing = self._entries[self._index_by_url[key] - 1]
            if title and not existing["title"]:
                existing["title"] = title
            if publisher and not existing["publisher"]:
                existing["publisher"] = publisher
            return self._index_by_url[key]
        idx = len(self._entries) + 1
        self._index_by_url[key] = idx
        self._entries.append({
            "index": idx,
            "url": str(url).strip(),
            "title": (title or "").strip(),
            "publisher": (publisher or "").strip(),
        })
        return idx

    def entries(self) -> list[dict]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


def seed_registry() -> SourceRegistry:
    """Statistics and the injury feed are always cited, so they always get [1] and [2]."""
    registry = SourceRegistry()
    registry.add("https://collegefootballdata.com", "College Football Data API", "CollegeFootballData")
    registry.add("https://www.rotowire.com/cfootball/news.php",
                 "College football injury feed", "Injury feed")
    return registry


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _run_one(api_key: str, job: dict, ctx: dict, settings: dict | None = None) -> dict:
    """Execute a single research call. Never raises — a failure degrades to no_data."""
    settings = settings or config.default_settings()
    prompt = job.get("prompt") or _build_prompt(job, ctx)
    model = settings["research_model"]
    # Only send what this model accepts. A rejected parameter fails the whole
    # call, and a research call that fails is a section that silently comes
    # back empty — which is exactly what a Perplexity Sonar model did: it takes
    # neither response_format nor, on the base models, reasoning.
    caps = openrouter.capabilities(model)
    # A model that browses natively needs no plugin; attaching one would buy a
    # second, redundant search on top of the one it runs itself.
    plugins = None if caps["native_search"] else openrouter.web_search_plugin(
        settings,
        search_prompt=(
            "Live web results retrieved just now. Prefer the most recently published "
            "items and read the full page content before citing it:"
        ),
    )
    if not caps["structured_output"]:
        # The prompt already demands a bare JSON object and parse_json_lenient
        # copes with prose around it, so the schema is a bonus, not a crutch.
        logging.info(f"Research model {model} takes no response_format; "
                     f"relying on the prompt's JSON contract")
    try:
        resp = openrouter.chat(
            api_key,
            model,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            plugins=plugins,
            response_format=RESEARCH_SCHEMA if caps["structured_output"] else None,
            effort=settings["research_effort"] if caps["reasoning"] else None,
            max_tokens=settings["research_max_tokens"],
            timeout=config.RESEARCH_TIMEOUT,
        )
    except Exception as e:
        logging.warning(f"Research call '{job['key']}' failed: {e}")
        return {
            "findings": [],
            "no_data": True,
            "notes": f"Live web research unavailable for this section ({e.__class__.__name__}).",
            "error": str(e)[:300],
            "citations": [],
            "usage": {},
        }

    text = openrouter.extract_text(resp)
    parsed = openrouter.parse_json_lenient(text) or {}
    findings = parsed.get("findings")
    if not isinstance(findings, list):
        findings = []

    clean: list[dict] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        headline = (f.get("headline") or "").strip()
        detail = (f.get("detail") or "").strip()
        if not headline and not detail:
            continue
        clean.append({
            "headline": headline,
            "detail": detail,
            "player": (f.get("player") or "").strip(),
            "position": (f.get("position") or "").strip(),
            "team": (f.get("team") or "").strip(),
            "status": (f.get("status") or "").strip(),
            "impact": (f.get("impact") or "").strip(),
            "published": (f.get("published") or "").strip(),
            "confidence": (f.get("confidence") or "").strip().lower() or "medium",
            "source_name": (f.get("source_name") or "").strip(),
            "source_url": (f.get("source_url") or "").strip(),
        })

    result = {
        "findings": clean,
        "no_data": bool(parsed.get("no_data")) or not clean,
        "notes": (parsed.get("notes") or "").strip(),
        "as_of_utc": (parsed.get("as_of_utc") or "").strip(),
        # Citations the search engine actually returned, kept alongside the model's own
        # attributions so we can tell what it read from what it claimed.
        "citations": openrouter.extract_citations(resp),
        "usage": openrouter.extract_usage(resp),
    }
    if not clean and not parsed:
        # Model answered but not in JSON — keep the prose so the section is not silently empty.
        result["raw_text"] = text[:4000]
        result["notes"] = result["notes"] or "Model returned unstructured text; preserved verbatim."
    return result


def run_research(api_key: str, ctx: dict, settings: dict | None = None,
                 jobs: list[dict] | None = None) -> dict:
    """Run every research job concurrently. Returns {job_key: bucket}.

    `jobs` defaults to the matchup set; other report types pass their own list.
    """
    jobs = jobs if jobs is not None else RESEARCH_JOBS
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=config.RESEARCH_MAX_WORKERS) as pool:
        futures = {pool.submit(_run_one, api_key, job, ctx, settings): job for job in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                out[job["key"]] = fut.result()
            except Exception as e:
                logging.exception(f"Research job '{job['key']}' raised unexpectedly")
                out[job["key"]] = {
                    "findings": [], "no_data": True, "citations": [], "usage": {},
                    "notes": f"Research failed: {e}",
                }
    return out


def build_context(home_full, away_full, home_short, away_short, year, kickoff=None) -> dict:
    return {
        "home_full": home_full,
        "away_full": away_full,
        "home_short": home_short,
        "away_short": away_short,
        "year": year,
        "kickoff": kickoff,
        "now_utc": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# Bucket assembly — one section can have several independently-sourced inputs
# ---------------------------------------------------------------------------
def assemble_sections(research: dict, rotowire: dict, registry: SourceRegistry, ctx: dict,
                      settings: dict | None = None) -> dict:
    """Merge research output and Rotowire into per-section, per-source buckets.

    Every finding gets a citation index from the shared registry so the report model can
    cite it as [n] and our renderer can print the matching SOURCES entry.
    """
    research_model = (settings or config.default_settings())["research_model"]

    def _bucket(job_key: str) -> dict:
        raw = research.get(job_key) or {}
        items = []
        for f in raw.get("findings", []):
            idx = registry.add(f.get("source_url", ""), f.get("headline", ""), f.get("source_name", ""))
            item = dict(f)
            item["citation"] = f"[{idx}]" if idx else ""
            items.append(item)
        # Pages the search engine returned that the model did not explicitly attribute.
        extra = []
        for c in raw.get("citations", []):
            idx = registry.add(c.get("url", ""), c.get("title", ""), "")
            if idx:
                extra.append({"citation": f"[{idx}]", "title": c.get("title", ""), "url": c.get("url", "")})
        return {
            "source": f"Live web research ({research_model})",
            "retrieved_at_utc": raw.get("as_of_utc") or ctx["now_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "no_data": raw.get("no_data", True),
            "notes": raw.get("notes", ""),
            "findings": items,
            "pages_retrieved": extra,
            **({"raw_text": raw["raw_text"]} if raw.get("raw_text") else {}),
        }

    def _roto(items: list[dict]) -> dict:
        import injuries as injuries_mod

        # Each stored item carries the publisher and URL it was actually collected from,
        # so cite that rather than attributing everything to one aggregator.
        for it in items:
            idx = registry.add(it.get("source_url", ""),
                               it.get("headline") or "College football injury feed",
                               it.get("source_name") or "Injury feed")
            it["citation"] = f"[{idx}]" if idx else "[2]"
        return {
            "source": ("Stored injury feed — items collected for this team over the last "
                       f"{config.INJURY_WINDOW_DAYS} days and kept in the local database"),
            "window_days": config.INJURY_WINDOW_DAYS,
            "no_data": not items,
            # One current status per player, latest dated word wins — the timeline
            # below it stays intact, but the model no longer has to reconcile a
            # Tuesday "Questionable" against a Friday "Out" on its own.
            "current_availability": injuries_mod.resolve_availability(items),
            "items": items,
        }

    return {
        "home_injury_updates": {
            "section_title": f"{ctx['home_full']} Injury Updates",
            "inputs": {
                "live_web_research": _bucket("home_injuries"),
                "rotowire_feed": _roto(rotowire.get("home", [])),
            },
        },
        "away_injury_updates": {
            "section_title": f"{ctx['away_full']} Injury Updates",
            "inputs": {
                "live_web_research": _bucket("away_injuries"),
                "rotowire_feed": _roto(rotowire.get("away", [])),
            },
        },
        "key_player_matchups": {
            "section_title": "Key Player Matchups",
            "inputs": {"live_web_research": _bucket("key_player_matchups")},
        },
        "home_roster_updates": {
            "section_title": f"{ctx['home_full']} Roster Updates",
            "inputs": {"live_web_research": _bucket("home_roster")},
        },
        "away_roster_updates": {
            "section_title": f"{ctx['away_full']} Roster Updates",
            "inputs": {"live_web_research": _bucket("away_roster")},
        },
        "home_practice_updates": {
            "section_title": f"{ctx['home_full']} Practice and Scrimmage Updates",
            "inputs": {"live_web_research": _bucket("home_practice")},
        },
        "away_practice_updates": {
            "section_title": f"{ctx['away_full']} Practice and Scrimmage Updates",
            "inputs": {"live_web_research": _bucket("away_practice")},
        },
        "media_matchup_analysis": {
            "section_title": f"{ctx['home_full']} vs {ctx['away_full']} Media Matchup Analysis",
            "inputs": {"live_web_research": _bucket("media_analysis")},
        },
    }
