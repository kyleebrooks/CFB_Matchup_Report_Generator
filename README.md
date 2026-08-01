# CFB Matchup Report Generator

Generates a cited, data-backed college football matchup PDF with a quantitative score
prediction. Statistics come from CollegeFootballData; news comes from live web research;
the two are synthesized into one report.

## Pipeline

```
                 ┌────────────────────────────────────────────┐
POST             │ Stage 1 — run concurrently                 │
/generate-report │                                            │
                 │  CFBD (12 endpoints, parallel)             │  statistics only
                 │  Rotowire (local SQLite, filtered by team) │  injury feed
                 │  8 × DeepSeek V4 Flash calls (parallel)    │  live web + citations
                 └────────────────────────────────────────────┘
                                     │
                 ┌───────────────────▼────────────────────────┐
                 │ Stage 2 — assembly (pure Python)           │
                 │  source registry → stable [n] citations    │
                 │  national percentiles                      │
                 │  statistical baseline (margin, total, WP)  │
                 │  8 matplotlib charts → base64 PNG          │
                 └────────────────────────────────────────────┘
                                     │
                 ┌───────────────────▼────────────────────────┐
                 │ Stage 3 — one Kimi K3 call (no web search) │
                 │  writes all 20 sections from the bundle    │
                 └────────────────────────────────────────────┘
                                     │
                        markdown → HTML → wkhtmltopdf → watermark
```

### The eight research calls

Each news-driven section gets its own dedicated live-web call with its own recency
window and its own citations — one call per team where the section is team-specific:

| Section | Calls |
|---|---|
| Injury Updates | 2 (one per team) |
| Roster Updates (non-injury) | 2 (one per team) |
| Practice & Scrimmage Updates | 2 (one per team) |
| Key Player Matchups | 1 (matchup) |
| Media Matchup Analysis | 1 (matchup) |

Statistics are never researched by an LLM — CFBD is the only source for numbers.

### Multi-input sections

A section can have more than one input. Injuries, for example, carry both a
`live_web_research` bucket and a `rotowire_feed` bucket. Buckets stay separate all the
way through to the report, and the model is required to label them separately in the
prose rather than blending them.

### Citations

Every URL retrieved during research is registered once and assigned a stable index.
`[1]` is always CollegeFootballData and `[2]` is always Rotowire. The report model is
handed the pre-assigned markers and told to reuse them verbatim; the SOURCES list at the
end of the PDF is rendered by `render.py`, not by the model, so a citation can never
point at a URL that was hallucinated or dropped.

## Modules

| File | Responsibility |
|---|---|
| `app.py` | Flask routes, auth, CORS, Rotowire scheduler |
| `jobs.py` | Background job manager backing the async API |
| `pipeline.py` | The full generation run, decoupled from Flask |
| `config.py` | Every tunable, all env-overridable |
| `db.py` | MySQL key store, SQLite Rotowire feed, team-name matching |
| `cfbd.py` | CFBD fetching, normalization, percentiles, pruning |
| `openrouter.py` | OpenRouter client, web-search plugin, lenient JSON parsing |
| `research.py` | The 8 research jobs, prompts, source registry, bucket assembly |
| `predict.py` | Deterministic margin / total / win-probability baseline |
| `charts.py` | The 8 procedural charts |
| `report.py` | Final synthesis prompt and call |
| `render.py` | HTML assembly, PDF, watermark |
| `accounts.py` | Multi-tenant accounts, entitlements, per-account settings, watermarks |
| `api_v1.py` | The `/v1` REST API (reports, account self-service, admin) |
| `report_types.py` | Registry of report types — add a type here and the API picks it up |
| `team_report.py` | Single-team season report pipeline |
| `admin_tui.py` | SSH admin console (curses UI + CLI subcommands) |
| `admin_console.py` | Console screens as pure render functions — testable headlessly |
| `schema.py` | Declarative DB schema: audit, report gaps, repair additively |
| `envfile.py` | Loads the service environment when running outside systemd |
| `usage.py` | Per-account API call tracking |
| `dbbrowse.py` | Read-only browser for both databases |
| `catalog.py` | What each report contains and how each section is produced |
| `examples.py` | Copy-pasteable API examples, generated per account |
| `settings_store.py` | Service-wide setting overrides, live from the database |
| `scoreboard.php` | Reference frontend (lives on the web host, not the droplet) |
| `report_proxy.php` | Same-origin PHP proxy the frontend calls (web host) |

## Two APIs

| API | Base | Auth | Consumers |
|---|---|---|---|
| Legacy | `/` | single `SERVICE_API_KEY` | afplnapicks.com scoreboard |
| Multi-tenant | `/v1` | per-account key | CFBReports.com and other customers |

The legacy endpoints are unchanged. `/v1` adds per-account entitlements (which report
types a key may request), per-account model and search settings, per-account watermark
uploads, and admin endpoints for minting accounts. Full reference: **[API.md](API.md)**.

Report types live in `report_types.py`. Adding one means adding a registry entry and the
module that builds it — the API surface, entitlements, job handling and PDF delivery all
key off that table.

| Type | Sections |
|---|---|
| `matchup` | 20 sections, 8 charts, projected final score |
| `team` | Overall outlook, game-by-game schedule, practice, roster, injuries, media, coaches; 4 charts |

## Admin console

A full-screen terminal console over SSH. No extra dependencies — `curses` is stdlib.

```bash
ssh deploy@your-droplet
cd /opt/afplna && ./venv/bin/python admin_tui.py
```

Eight screens:

| # | Screen | What it does |
|---|---|---|
| 1 | Dashboard | Service, database, accounts, effective settings, paths |
| 2 | Accounts | Create, delete, rotate keys, entitlements, per-account settings, activate, watermark, call counts |
| 3 | Settings | Service-wide overrides, live |
| 4 | Database | Schema audit and additive repair |
| 5 | Health | Same checks as `GET /health`, in-process |
| 6 | Browser | Read-only walk through both databases |
| 7 | Reports | Every report's sections and how each one is produced |
| 8 | Examples | Copy-pasteable API calls, generated per account |

Every screen paints its own keys in the footer, above the global bar, so the available
actions are never more than a glance away and can never scroll out of view. `?` opens
the full key map. On the Accounts screen:

| Key | Action | | Key | Action |
|---|---|---|---|---|
| `n` | new account | | `t` | activate / deactivate |
| `e` | edit report entitlements | | `m` | toggle admin |
| `s` | edit a per-account setting | | `w` | clear the custom watermark |
| `k` | issue a new API key | | `D` | delete permanently |
| `ENTER` | account detail | | `ESC` | back to the list |

The account keys work in the detail view as well as the list, so opening an account is
never a dead end. In any input, the suggested value is replaced by the first key you
type; `^U` clears the line and `ESC` cancels.

**Usage tracking** — one row per API report request in `report_usage`, written when the
job is queued and closed out when it finishes. The account list shows lifetime and
30-day call counts; account detail adds completions, failures and recent request
history. Accounts also see their own numbers at `GET /v1/account/usage`. Every usage
write is best-effort — accounting never fails a customer's report.

**Browser** — walks the MySQL tables and the Rotowire SQLite side by side. Strictly
read-only: table names are validated against the live catalog before use, and key,
hash and password columns render as `<redacted>`.

**Report catalog** — assembled from the live definitions, so it cannot drift from what
the service actually does. Each section is labelled with its source: live web research,
research plus the Rotowire feed, CFBD statistics, or synthesis.

**Examples** — built from the account's real entitlements, so an unentitled report type
never appears. Keys are never embedded; examples reference `$KEY`.

### Running it from a shell

systemd feeds the service `/etc/afplna.env` via `EnvironmentFile=`; an interactive SSH
shell gets none of that, so a hand-run console would otherwise fail with
`Access denied for user ... (using password: NO)` — which blames the database for a
missing environment.

The console fills that gap itself, trying in order: the environment already in the
shell, the env file if readable (works under `sudo`), then the **running service's own
environment via `/proc/<pid>/environ`** — which the `deploy` user can read without sudo
and without loosening any file permissions. That last path is the one that normally
fires.

If none work it refuses with the four ways to fix it rather than surfacing a database
error. `admin_tui.py env` shows which source was used.

The check is probe-based: it only refuses when the database is *actually* unreachable,
so a differently-configured or passwordless database is never blocked on a proxy signal.

Everything is also available non-interactively, for cron or a terminal that cannot do
curses:

```bash
./venv/bin/python admin_tui.py audit --apply     # audit and repair the schema
./venv/bin/python admin_tui.py accounts          # list
./venv/bin/python admin_tui.py create "Name" team,matchup
./venv/bin/python admin_tui.py rotate 3
./venv/bin/python admin_tui.py settings
./venv/bin/python admin_tui.py set report_effort medium
./venv/bin/python admin_tui.py unset report_effort
```

### Settings layering

```
config.default_settings()   environment / code defaults
report_service_settings     service-wide overrides   <- admin console, live
report_accounts.settings    per-account overrides    <- console or PATCH /v1/account/settings
```

Service-wide and per-account changes take effect immediately — no restart. Settings that
only exist in the environment (DB credentials, `ADMIN_API_KEY`, timeouts, paths) are
shown read-only, since changing them needs an `/etc/afplna.env` edit and a restart.

### Schema audit

`schema.py` holds a declarative spec of the tables the service needs. The audit compares
it against `INFORMATION_SCHEMA` and reports missing tables, columns and indexes, plus
which `API_KEYS` rows are present — presence only, key values are never displayed.

Repairs are **additive only**: `CREATE TABLE`, `ADD COLUMN`, `ADD INDEX`. Nothing drops,
renames or narrows anything, so it is safe against a database with live data. `API_KEYS`
predates this service and is audited but never altered.

## Search engine and the research model

The research model defaults to **DeepSeek V4 Flash**, which has **no native web search**.
`OPENROUTER_SEARCH_ENGINE` therefore defaults to `exa` — OpenRouter's Exa-backed engine,
which works for any model. Setting it to `native` with a DeepSeek model would silently
lose the browsing the whole report depends on.

Exa bills per result (~$4/1,000), so `search_max_results` is the main cost lever; it
defaults to 5 and is overridable per account.

## API keys

Keys are read from the MySQL `API_KEYS` table first, then from the environment.
**OpenRouter is the only LLM provider** — there are no direct calls to any model vendor.

```sql
INSERT INTO API_KEYS (`API_NAME`, `KEY`) VALUES ('openrouter', 'sk-or-v1-...');
```

or, in `/etc/afplna.env`:

```bash
OPENROUTER_API_KEY='sk-or-v1-...'
```

Verify both models respond:

```bash
curl -sS "https://your-host/health/llm?api_key=$SERVICE_API_KEY" | jq
```

Any legacy `openai` row in `API_KEYS` is unused and can be deleted.

### The web-host proxy

`scoreboard.php` and `report_proxy.php` run on the **web host** (GoDaddy), not the
droplet. The site is HTTPS and the droplet is plain HTTP, so the browser cannot call the
droplet directly — mixed content is blocked. `report_proxy.php` bridges that: the page
calls it same-origin over HTTPS, and it forwards server-side over HTTP. It also keeps
the service API key off the page and enforces the members-only session.

Two failure modes look identical in the browser and are fixed in different places:

- **Really logged out** — no session cookie was sent.
- **Session not loaded** — the cookie arrived, but the proxy opened a *different*
  session than the rest of the site. This happens when the proxy calls `session_start()`
  cold while the site bootstraps through `common.inc` with a custom session name or
  cookie path. The proxy now mirrors the site's bootstrap first.

Its 401 body carries a `debug` block (`session_name`, `cookie_received`, `session_keys`)
that tells the two apart, and the page logs it to the console.

### 502 Bad Gateway

Nginx returns 502 when Gunicorn is not running, i.e. the app failed at **import** time.
No route was involved. Reproduce it directly for the real traceback:

```bash
cd /opt/afplna && sudo -u deploy /opt/afplna/venv/bin/python -c "import app"
```

Usually dependencies were not reinstalled after a pull. A missing matplotlib no longer
takes the service down — the app starts, `/health` reports `charts.ok: false`, and
report generation fails with "Charting library missing on the server" instead of every
endpoint returning 502.

### Reasoning models and the token budget

Kimi K3 is a reasoning model, and reasoning tokens count against `max_tokens`. Set the
budget too low and the model spends all of it thinking, then returns **empty content**
with `finish_reason: "length"` — which looks like a broken model but is a budget
problem. `REPORT_MAX_TOKENS` defaults to 96000 to cover both the reasoning trace and
the ~12k-token report, and an empty reply is reported as exactly that, with the
reasoning-token count, rather than a generic failure.

The same applies to health checks: probing a reasoning model with a tiny `max_tokens`
returns an empty reply and looks like an outage. `/health` uses 2048.

### When a report fails

`GET /health?api_key=…` checks every dependency in one call and is the first thing to
run. A rejected CFBD key is reported explicitly rather than being swallowed into a
report full of empty statistics sections — CFBD answers a bad key with HTTP 401 and the
body `{"message":"You must be logged in"}`, which surfaces as
*"CollegeFootballData rejected the API key"*, naming the failing endpoints.

`GET /health/cfbd?api_key=…` probes each CFBD endpoint **sequentially**, one request at
a time. That separates two failures that look identical in the logs:

- **Key or tier rejection** — that endpoint 401/403s even when probed alone. `/lines`
  is the usual suspect; betting lines are gated behind a paid CFBD tier, and a single
  gated endpoint degrades gracefully rather than failing the report.
- **Rate limiting** — the endpoint passes when probed alone but fails during a report.
  The report fires ~25 requests at once where the pre-rewrite code made 20 sequentially,
  so 429s are now retried with backoff and concurrency is capped at
  `CFBD_MAX_WORKERS` (default 4).

## Configuration

All of `config.py` is env-overridable. The ones worth knowing:

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_RESEARCH_MODEL` | `openai/gpt-5.6-luna` | the 8 web-research calls |
| `OPENROUTER_REPORT_MODEL` | `moonshotai/kimi-k3` | the single report call |
| `OPENROUTER_SEARCH_ENGINE` | `native` | `native`, `exa`, or blank to let OpenRouter pick |
| `OPENROUTER_SEARCH_MAX_RESULTS` | `10` | Exa billing is per result |
| `RESEARCH_TIMEOUT` / `REPORT_TIMEOUT` | `240` / `420` | seconds |
| `REPORT_MAX_TOKENS` | `96000` | must cover reasoning **and** the report — see below |
| `REPORT_EFFORT` | `high` | lower this before lowering the token budget |
| `HOME_FIELD_ADVANTAGE` | `2.4` | points added to every rating differential |
| `MARGIN_STDDEV` | `13.5` | drives the win-probability curve |
| `TOP_PLAYERS_PER_TEAM` | `18` | player-PPA pruning before the report prompt |
| `CFBD_MAX_WORKERS` | `4` | concurrent CFBD requests; raise carefully, CFBD rate-limits |
| `ROTOWIRE_DB_PATH` | `./rotowire.db` | local SQLite feed |

## Visuals

Eight charts, the same eight on every report, rendered headless with matplotlib and
embedded as base64 PNGs (wkhtmltopdf's WebKit cannot run JS charting):

1. Power Rating Dashboard — SP+ components, FPI, Elo
2. Efficiency Profile — radar, national percentiles
3. Mismatch Matrix — each offense against the defense it will face
4. Season Form Trend — per-game offensive and defensive PPA
5. Top Individual Impact — players ranked by total PPA
6. Roster Continuity & Talent — returning production and recruiting composite
7. Projected Margin by System — SP+, FPI, Elo, market line, consensus
8. Win Probability Distribution

Series use each school's official color from CFBD, falling back automatically when the
two teams' colors are too similar to tell apart. A chart with no usable data renders a
styled placeholder of identical size, so page layout never shifts between reports.

## Prediction

`predict.py` computes a projected margin from each rating system independently, blends
them, and — when CFBD returns a betting line for the game — blends that in 50/50. The
report model receives this as an anchor it must state explicitly and then justify any
adjustment away from, based on injuries, roster news and matchup edges. Reports are
graded on how close the final prediction lands to the real score.

## Timing and the async job API

A report takes roughly 3–6 minutes — far longer than a browser or proxy will hold a
connection open. `POST /generate-report` therefore returns **202 immediately** with a job
handle, and the client polls `GET /report-status` for staged progress.

```
POST /generate-report            -> 202 {"job_id":"…","state":"queued","percent":0}
GET  /report-status?home_team=…  -> {"state":"running","percent":58,
                                     "message":"Rendering charts","elapsed_seconds":94,
                                     "report_exists":true}
                                 -> {"state":"done","percent":100,
                                     "result":{"filename":…,"seconds":…,"sources":…,
                                               "projected_score":{…}}}
                                 -> {"state":"error","error":…,"detail":…}
```

`state` is `queued`, `running`, `done`, `error`, or `none` (nothing queued this process
lifetime). `report_exists` always reflects what is on disk, so the UI can offer a
download even after a restart cleared the job table.

Notes:
- Posting twice for the same matchup while a build is in flight returns the **existing**
  job rather than starting a second one.
- The new PDF is built to a `.building` temp file and swapped in at the end, so the
  previous report for that matchup stays downloadable for the whole rebuild.
- Job state is in-process memory. This is correct **only** while Gunicorn runs
  `--workers 1` (which it must anyway, or the Rotowire scheduler double-fires). Raising
  the worker count means moving this to Redis or the database.
- `POST /generate-report?wait=true` still runs synchronously and returns 200 with the
  result — convenient for curl and cron, where the caller owns the timeout.

Gunicorn and Nginx timeouts must still be at least 900s for the `wait=true` path (see
`setup_instructions.txt`).

## Endpoints

| Route | Method | Purpose |
|---|---|---|
| `/generate-report` | POST | Queue a report; 202 + job handle. Body: `home_full`, `away_full`, `home_short`, `away_short`, optional `year`, `kickoff`, `wait` |
| `/report-status` | GET | Job state, stage, percent, elapsed, result or error |
| `/get-report` | GET | Download the latest PDF for a matchup |
| `/has-report` | GET | Whether a report exists |
| `/health` | GET | Check everything: CFBD key, OpenRouter key + both models, Rotowire DB, wkhtmltopdf, reports dir |
| `/health/cfbd` | GET | Probe every CFBD endpoint individually (optional `year`, `team`) |
| `/health/llm` | GET | OpenRouter only — resolve the key and ping both models |
| `/ping` | GET | Liveness |

## Local Rotowire database

The application writes Rotowire articles to a local SQLite database. By default the file
`rotowire.db` is created in the project root. To place it elsewhere:

```bash
export ROTOWIRE_DB_PATH=/var/cfb/rotowire.db
```

Make sure the process has read and write access to the directory.
