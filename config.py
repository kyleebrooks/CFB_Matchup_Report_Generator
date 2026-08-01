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
WATERMARK_PATH = os.path.join(BASE_DIR, 'AFPLNA_LOGO.png')

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

# Settings an account is allowed to override. Anything else in a PATCH is rejected
# rather than silently ignored.
ACCOUNT_SETTING_KEYS = (
    'research_model',
    'report_model',
    'search_engine',
    'search_max_results',
    'research_effort',
    'report_effort',
    'research_max_tokens',
    'report_max_tokens',
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
        'search_engine': OPENROUTER_SEARCH_ENGINE,
        'search_max_results': OPENROUTER_SEARCH_MAX_RESULTS,
        'research_effort': RESEARCH_EFFORT,
        'report_effort': REPORT_EFFORT,
        'research_max_tokens': RESEARCH_MAX_TOKENS,
        'report_max_tokens': REPORT_MAX_TOKENS,
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
