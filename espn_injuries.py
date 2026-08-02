"""ESPN's public injury listing — the feed's structured second source.

The research collector is thorough but probabilistic: one model, one search engine,
one pass. This provider is the opposite — a deterministic list of current injury
designations straight from ESPN's public JSON API, at zero LLM cost. Neither source
is trusted alone; rows from both land in the feed with their provider recorded, and
the per-player availability resolution picks the freshest word.

Everything here is fail-soft: ESPN is undocumented public JSON, so any surprise —
timeouts, moved endpoints, missing fields — degrades to an empty list, never an error
that could block a report or a sweep.
"""

import logging
import threading
import time

import requests

TEAMS_URL = ('https://site.api.espn.com/apis/site/v2/sports/football/'
             'college-football/teams?limit=1000')
INJURIES_URL = ('https://sports.core.api.espn.com/v2/sports/football/leagues/'
                'college-football/teams/{team_id}/injuries?limit=50')

TIMEOUT = 10
MAX_ITEMS_PER_TEAM = 15          # bounds the $ref-chasing; ESPN lists are short anyway
_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; CFBMatchupReport/1.0)'}

# School-name -> ESPN team id, cached for a day: the mapping changes once a season.
_team_map: dict = {}
_team_map_at = 0.0
_team_map_lock = threading.Lock()
TEAM_MAP_TTL = 24 * 3600


def _get_json(url: str):
    """One GET, JSON or None. Never raises."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            logging.debug(f"ESPN {resp.status_code} for {url}")
            return None
        return resp.json()
    except Exception as e:
        logging.debug(f"ESPN request failed ({e.__class__.__name__}): {url}")
        return None


def _normalize(name: str) -> str:
    return ' '.join((name or '').lower().replace('&', 'and').split())


def _load_team_map() -> dict:
    """school-name variants (normalised) -> ESPN team id."""
    global _team_map, _team_map_at
    with _team_map_lock:
        if _team_map and time.time() - _team_map_at < TEAM_MAP_TTL:
            return _team_map
        data = _get_json(TEAMS_URL) or {}
        mapping: dict = {}
        try:
            leagues = (data.get('sports') or [{}])[0].get('leagues') or [{}]
            for entry in leagues[0].get('teams') or []:
                team = entry.get('team') or {}
                team_id = team.get('id')
                if not team_id:
                    continue
                # "location" is the school ("Georgia"); displayName adds the mascot.
                for key in ('location', 'displayName', 'shortDisplayName', 'nickname'):
                    name = _normalize(team.get(key) or '')
                    if name:
                        mapping.setdefault(name, str(team_id))
        except Exception as e:
            logging.debug(f"ESPN team list unparseable: {e}")
        if mapping:
            _team_map, _team_map_at = mapping, time.time()
        return mapping


def team_id(school_short: str, school_full: str = '') -> str | None:
    mapping = _load_team_map()
    for candidate in (school_short, school_full):
        found = mapping.get(_normalize(candidate or ''))
        if found:
            return found
    return None


def _resolve(ref):
    """Follow a {'$ref': url} link. Returns {} on anything unexpected."""
    if isinstance(ref, dict) and ref.get('$ref'):
        return _get_json(ref['$ref']) or {}
    return ref if isinstance(ref, dict) else {}


def fetch_team_injuries(school_short: str, school_full: str = '') -> list[dict]:
    """Current ESPN injury designations for one school, shaped like research findings.

    The list is ESPN's CURRENT injuries — presence on it means the designation holds
    right now — so items are published "today" and the original injury date, when
    ESPN provides one, is kept inside the detail text.
    """
    from datetime import datetime

    tid = team_id(school_short, school_full)
    if not tid:
        return []
    listing = _get_json(INJURIES_URL.format(team_id=tid)) or {}
    items = listing.get('items') or []
    today = datetime.now().date().isoformat()

    findings = []
    for ref in items[:MAX_ITEMS_PER_TEAM]:
        item = _resolve(ref)
        if not item:
            continue
        athlete = _resolve(item.get('athlete'))
        player = (athlete.get('displayName') or athlete.get('fullName') or '').strip()
        position = ((athlete.get('position') or {}).get('abbreviation') or '').strip()
        status = (item.get('status') or '').strip()
        if isinstance(item.get('type'), dict):
            status = status or (item['type'].get('description') or '').strip()
        comment = (item.get('longComment') or item.get('shortComment') or '').strip()
        details = item.get('details') or {}
        detail_bits = [comment] if comment else []
        if isinstance(details, dict):
            kind = (details.get('type') or '').strip()
            side = (details.get('side') or '').strip()
            if kind:
                detail_bits.append(f"Injury: {(side + ' ') if side else ''}{kind}.")
            if details.get('returnDate'):
                detail_bits.append(f"Listed return date: {details['returnDate']}.")
        when = (item.get('date') or '')[:10]
        if when:
            detail_bits.append(f"First listed {when}.")
        if not player and not detail_bits:
            continue
        findings.append({
            'headline': f"{player or 'Roster'} — {status or 'Injury listed'} (ESPN)",
            'detail': ' '.join(detail_bits) or f"Listed {status or 'injured'} by ESPN.",
            'player': player,
            'position': position,
            'team': school_full or school_short,
            'status': status,
            'impact': '',
            'published': today,       # the designation is current as of this fetch
            'confidence': 'high',
            'source_name': 'ESPN',
            'source_url': f'https://www.espn.com/college-football/team/injuries/_/id/{tid}',
        })
    return findings
