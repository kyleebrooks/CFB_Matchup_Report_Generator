"""Central configuration for the AFPLNA CFB Matchup Report Generator.

Every tunable lives here so the service can be re-pointed (different model, different
search engine, longer timeouts) by editing /etc/afplna.env and restarting, with no
code deploy.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_HOST = os.getenv('DB_HOST', 'p3nlmysql149plsk.secureserver.net')
DB_USER = os.getenv('DB_USER', 'kdogg4207')
DB_NAME = os.getenv('DB_NAME', 'kdogg4207')
DB_PASSWORD = os.getenv('DB_PASSWORD')

# Local Rotowire DB path; defaults to 'rotowire.db' in the project root
ROTOWIRE_DB_PATH = os.getenv('ROTOWIRE_DB_PATH', os.path.join(os.getcwd(), 'rotowire.db'))

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
SERVICE_API_KEY = os.getenv('SERVICE_API_KEY')
WKHTMLTOPDF_PATH = os.getenv('WKHTMLTOPDF_PATH')  # /usr/bin/wkhtmltopdf
REPORTS_DIR = os.getenv('REPORTS_DIR', os.path.join(BASE_DIR, 'reports'))
PODCASTS_DIR = os.getenv('PODCASTS_DIR', os.path.join(BASE_DIR, 'podcasts'))
WATERMARK_PATH = os.path.join(BASE_DIR, 'AFPLNA_LOGO.png')

# ---------------------------------------------------------------------------
# Voice studio — external VibeVoice workstations that render episodes for us
# ---------------------------------------------------------------------------
# A shared token, deliberately separate from any account API key: the workstation
# polling this queue lives on someone's desk, and a leak there must not hand out the
# ability to read reports or spend money on report generation. Unset disables the
# worker endpoints entirely.
VOICE_WORKER_TOKEN = os.getenv('VOICE_WORKER_TOKEN')
# How long a studio may hold a claimed job without reporting progress before it goes
# back in the queue. Long, because one render stage can legitimately take minutes.
VOICE_JOB_LEASE_SECONDS = int(os.getenv('VOICE_JOB_LEASE_SECONDS', '900'))
# Largest episode a studio may post back.
VOICE_JOB_MAX_UPLOAD_MB = int(os.getenv('VOICE_JOB_MAX_UPLOAD_MB', '200'))

# ---------------------------------------------------------------------------
# CollegeFootballData (CFBD)
# ---------------------------------------------------------------------------
CFBD_BASE_URL = os.getenv('CFBD_BASE_URL', 'https://api.collegefootballdata.com')
CFBD_TIMEOUT = int(os.getenv('CFBD_TIMEOUT', '30'))
# Kept modest on purpose: CFBD rate-limits, and the report fires ~25 requests.
CFBD_MAX_WORKERS = int(os.getenv('CFBD_MAX_WORKERS', '4'))

# ---------------------------------------------------------------------------
# OpenRouter — the single gateway for every LLM call in this service
# ---------------------------------------------------------------------------
OPENROUTER_BASE_URL = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')

# Stage 1: parallel live-web research calls.
OPENROUTER_RESEARCH_MODEL = os.getenv('OPENROUTER_RESEARCH_MODEL', 'deepseek/deepseek-v4-flash')
# Stage 2: one synthesis call that writes the finished report.
OPENROUTER_REPORT_MODEL = os.getenv('OPENROUTER_REPORT_MODEL', 'moonshotai/kimi-k3')
# The premium tier swaps only the synthesis model. Defaulting to the standard report
# model means premium behaves identically until an operator actually picks a bigger
# model — no surprise spend from a deploy alone.
OPENROUTER_PREMIUM_REPORT_MODEL = os.getenv('OPENROUTER_PREMIUM_REPORT_MODEL',
                                            OPENROUTER_REPORT_MODEL)

# Web search plugin. "native" routes OpenAI/Anthropic/Google/xAI models to the
# provider's own live browsing; "exa" forces OpenRouter's Exa-backed search; ""
# lets OpenRouter pick (native where supported, Exa otherwise).
#
# DeepSeek has NO native web search, so the research model must go through Exa.
# Leaving this on "native" with a DeepSeek model would silently lose the browsing
# the whole report depends on.
OPENROUTER_SEARCH_ENGINE = os.getenv('OPENROUTER_SEARCH_ENGINE', 'exa')
# Exa bills per result ($4/1000). Five results still yield 2-4k characters of extract
# each, which is ample per topic; accounts can raise this individually.
OPENROUTER_SEARCH_MAX_RESULTS = int(os.getenv('OPENROUTER_SEARCH_MAX_RESULTS', '5'))

# Sent as HTTP-Referer / X-Title so the calls are attributable in the OpenRouter dashboard.
OPENROUTER_REFERER = os.getenv('OPENROUTER_REFERER', 'https://afplnapicks.com')
OPENROUTER_APP_TITLE = os.getenv('OPENROUTER_APP_TITLE', 'AFPLNA CFB Matchup Report Generator')

RESEARCH_TIMEOUT = int(os.getenv('RESEARCH_TIMEOUT', '240'))
RESEARCH_MAX_WORKERS = int(os.getenv('RESEARCH_MAX_WORKERS', '8'))
RESEARCH_MAX_TOKENS = int(os.getenv('RESEARCH_MAX_TOKENS', '8000'))
RESEARCH_EFFORT = os.getenv('RESEARCH_EFFORT', 'medium')

REPORT_TIMEOUT = int(os.getenv('REPORT_TIMEOUT', '420'))
# Reasoning models bill thinking against max_tokens, so this has to cover BOTH the
# reasoning trace and the ~12k-token report. Too low and the model thinks until the
# budget is gone and returns empty content.
REPORT_MAX_TOKENS = int(os.getenv('REPORT_MAX_TOKENS', '96000'))
REPORT_EFFORT = os.getenv('REPORT_EFFORT', 'high')

# ---------------------------------------------------------------------------
# Report tuning
# ---------------------------------------------------------------------------
# Recency windows enforced in every research prompt.
INJURY_WINDOW_DAYS = int(os.getenv('INJURY_WINDOW_DAYS', '14'))
ROSTER_WINDOW_DAYS = int(os.getenv('ROSTER_WINDOW_DAYS', '14'))
PRACTICE_WINDOW_DAYS = int(os.getenv('PRACTICE_WINDOW_DAYS', '7'))
MEDIA_WINDOW_DAYS = int(os.getenv('MEDIA_WINDOW_DAYS', '14'))
ROTOWIRE_WINDOW_DAYS = int(os.getenv('ROTOWIRE_WINDOW_DAYS', '7'))
# How old the newest feed row may get before the feed is called stale.
ROTOWIRE_STALE_DAYS = int(os.getenv('ROTOWIRE_STALE_DAYS', '3'))
# How long a team's injury rows stay usable before a report triggers a fresh lookup.
# This is the cost dial: lower means more up-to-the-minute, more search calls.
INJURY_FEED_TTL_HOURS = float(os.getenv('INJURY_FEED_TTL_HOURS', '6'))
# A failed refresh should not block retries for the whole TTL — stale-with-an-error is
# the one state worth paying to escape quickly.
INJURY_RETRY_HOURS = float(os.getenv('INJURY_RETRY_HOURS', '1'))
# Injury collection digs deeper than general research: designations hide in beat
# reporting that rarely tops the results.
INJURY_SEARCH_MAX_RESULTS = int(os.getenv('INJURY_SEARCH_MAX_RESULTS', '8'))
# ESPN's public injury listing — a free, structured second source alongside research.
INJURY_ESPN_ENABLED = os.getenv('INJURY_ESPN_ENABLED', '1') not in ('0', 'false', 'no')
# The scheduled game-aware sweep: "dow:hour_utc" slots (0=Mon). The defaults land after
# the midweek conference availability reports and on gameday morning (US time).
INJURY_SWEEP_ENABLED = os.getenv('INJURY_SWEEP_ENABLED', '1') not in ('0', 'false', 'no')
INJURY_SWEEP_SLOTS = os.getenv('INJURY_SWEEP_SLOTS', '2:23,5:13')
# Which games the sweep cares about: kickoffs inside this many days.
INJURY_SWEEP_LOOKAHEAD_DAYS = int(os.getenv('INJURY_SWEEP_LOOKAHEAD_DAYS', '4'))
# Flag any swept team whose feed is still failed/stale this close to kickoff.
KICKOFF_ALERT_HOURS = float(os.getenv('KICKOFF_ALERT_HOURS', '48'))
# Rough US-dollar cost of one web-search-backed research call, used only to warn before
# a full-FBS sweep. OpenRouter bills the Exa engine at about $4 per 1000 results.
SEARCH_COST_PER_CALL = float(
    os.getenv('SEARCH_COST_PER_CALL', str(0.004 * int(os.getenv('OPENROUTER_SEARCH_MAX_RESULTS', '5'))))
)

# How many players per team survive pruning before the stats blob goes to the report model.
TOP_PLAYERS_PER_TEAM = int(os.getenv('TOP_PLAYERS_PER_TEAM', '18'))

# Home-field advantage, in points, applied to every rating-differential baseline.
HOME_FIELD_ADVANTAGE = float(os.getenv('HOME_FIELD_ADVANTAGE', '2.4'))
# Standard deviation of CFB game margin vs. projection; drives the win-probability curve.
MARGIN_STDDEV = float(os.getenv('MARGIN_STDDEV', '13.5'))
# Elo points per point of scoring margin (standard CFB conversion).
ELO_POINTS_PER_MARGIN = float(os.getenv('ELO_POINTS_PER_MARGIN', '25.0'))

# ---------------------------------------------------------------------------
# Multi-tenant API (CFBReports.com and other consumers)
# ---------------------------------------------------------------------------
# Bootstrap admin key. Whoever holds this can mint accounts, so it lives in the env
# file (chmod 600) rather than the database.
ADMIN_API_KEY = os.getenv('ADMIN_API_KEY')

# Per-account watermark uploads.
WATERMARKS_DIR = os.getenv('WATERMARKS_DIR', os.path.join(BASE_DIR, 'watermarks'))
MAX_WATERMARK_BYTES = int(os.getenv('MAX_WATERMARK_BYTES', str(5 * 1024 * 1024)))
ALLOWED_WATERMARK_TYPES = ('image/png', 'image/jpeg', 'image/webp')
# How strongly the watermark is stamped. The default suits a solid, full-contrast logo:
# the renderer does the fading. An image that is ALREADY faint needs a higher value —
# 0.09 on top of a 5%-grey source leaves nothing visible at all.
WATERMARK_OPACITY = float(os.getenv('WATERMARK_OPACITY', '0.09'))
# Fraction of the page the mark spans, longest edge.
WATERMARK_SCALE = float(os.getenv('WATERMARK_SCALE', '0.92'))

# Whether the closing document furniture is included in the PDF: the numbered source
# list, and the "Generation details" panel (models, token counts, timings). On by
# default; a white-label account can turn either off.
INCLUDE_SOURCES = os.getenv('INCLUDE_SOURCES', '1').lower() not in ('0', 'false', 'no')
INCLUDE_GENERATION_DETAILS = os.getenv(
    'INCLUDE_GENERATION_DETAILS', '1').lower() not in ('0', 'false', 'no')

# Settings an account is allowed to override. Anything else in a PATCH is rejected
# rather than silently ignored.
ACCOUNT_SETTING_KEYS = (
    'research_model',
    'report_model',
    'premium_report_model',
    'search_engine',
    'search_max_results',
    'research_effort',
    'report_effort',
    'research_max_tokens',
    'report_max_tokens',
    'watermark_opacity',
    'watermark_scale',
    'include_sources',
    'include_generation_details',
)

# Optional guard rail: comma-separated allowlist of model ids accounts may select.
# Empty means any OpenRouter model is permitted.
_allow = os.getenv('ALLOWED_ACCOUNT_MODELS', '').strip()
ALLOWED_ACCOUNT_MODELS = tuple(m.strip() for m in _allow.split(',') if m.strip())


def default_settings() -> dict:
    """Service-wide defaults. Account overrides are layered on top of this."""
    return {
        'research_model': OPENROUTER_RESEARCH_MODEL,
        'report_model': OPENROUTER_REPORT_MODEL,
        'premium_report_model': OPENROUTER_PREMIUM_REPORT_MODEL,
        'search_engine': OPENROUTER_SEARCH_ENGINE,
        'search_max_results': OPENROUTER_SEARCH_MAX_RESULTS,
        'research_effort': RESEARCH_EFFORT,
        'report_effort': REPORT_EFFORT,
        'research_max_tokens': RESEARCH_MAX_TOKENS,
        'report_max_tokens': REPORT_MAX_TOKENS,
        'watermark_opacity': WATERMARK_OPACITY,
        'watermark_scale': WATERMARK_SCALE,
        'include_sources': 1 if INCLUDE_SOURCES else 0,
        'include_generation_details': 1 if INCLUDE_GENERATION_DETAILS else 0,
    }


# ---------------------------------------------------------------------------
# House chart style — identical on every report
# ---------------------------------------------------------------------------
CHART_DPI = int(os.getenv('CHART_DPI', '150'))
CHART_FACE = '#ffffff'
CHART_GRID = '#d9dde3'
CHART_TEXT = '#1f2933'
CHART_MUTED = '#6b7280'
CHART_FALLBACK_HOME = '#1f4e79'
CHART_FALLBACK_AWAY = '#a3232b'
