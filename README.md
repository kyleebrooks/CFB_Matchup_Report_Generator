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
                 │  8 × GPT-5.6 Luna research calls (parallel)│  live web + citations
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

## Configuration

All of `config.py` is env-overridable. The ones worth knowing:

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_RESEARCH_MODEL` | `openai/gpt-5.6-luna` | the 8 web-research calls |
| `OPENROUTER_REPORT_MODEL` | `moonshotai/kimi-k3` | the single report call |
| `OPENROUTER_SEARCH_ENGINE` | `native` | `native`, `exa`, or blank to let OpenRouter pick |
| `OPENROUTER_SEARCH_MAX_RESULTS` | `10` | Exa billing is per result |
| `RESEARCH_TIMEOUT` / `REPORT_TIMEOUT` | `240` / `420` | seconds |
| `HOME_FIELD_ADVANTAGE` | `2.4` | points added to every rating differential |
| `MARGIN_STDDEV` | `13.5` | drives the win-probability curve |
| `TOP_PLAYERS_PER_TEAM` | `18` | player-PPA pruning before the report prompt |
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
| `/health/llm` | GET | Resolve the OpenRouter key and ping both models |
| `/ping` | GET | Liveness |

## Local Rotowire database

The application writes Rotowire articles to a local SQLite database. By default the file
`rotowire.db` is created in the project root. To place it elsewhere:

```bash
export ROTOWIRE_DB_PATH=/var/cfb/rotowire.db
```

Make sure the process has read and write access to the directory.
