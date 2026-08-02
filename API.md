# AFPLNA Report Service — API Reference

Two independent APIs run side by side on the same host.

| API | Base | Auth | Consumers |
|---|---|---|---|
| **Legacy** | `/` | `SERVICE_API_KEY` (one shared key) | afplnapicks.com scoreboard |
| **Multi-tenant** | `/v1` | Per-account API key | CFBReports.com and other customers |

The legacy endpoints are unchanged. Nothing in `/v1` affects them.

---

# Part 1 — Multi-tenant API (`/v1`)

## Authentication

Send your key one of three ways (in precedence order):

```http
Authorization: Bearer cfbr_xxxxxxxxxxxxxxxxxxxx
X-Api-Key: cfbr_xxxxxxxxxxxxxxxxxxxx
```
```
?api_key=cfbr_xxxxxxxxxxxxxxxxxxxx        # or "api_key" in a JSON body
```

Keys are stored as SHA-256 hashes. A key is shown **once**, at creation or rotation, and cannot be recovered — only rotated.

| Status | Meaning |
|---|---|
| `401` | No key, unknown key, or deactivated account |
| `403` | Valid key, but not entitled to that report type / not an admin |
| `404` | Job does not exist **or** belongs to another account (deliberately indistinguishable) |
| `409` | Report not finished yet |
| `410` | Report finished but the PDF has since been removed from disk |

---

## Reports

### `GET /v1/report-types`
Every report type, flagged with whether your key may request it.

```bash
curl -sS https://HOST/v1/report-types -H "X-Api-Key: $KEY"
```
```json
{
  "report_types": [
    {"report_type": "matchup", "title": "Head-to-Head Matchup Report",
     "required_params": ["home_full","away_full","home_short","away_short"],
     "optional_params": ["year","kickoff"], "allowed": true},
    {"report_type": "team", "title": "Single-Team Season Report",
     "required_params": ["team_short"], "optional_params": ["team_full","year"],
     "allowed": true}
  ],
  "allowed_reports": ["matchup", "team"]
}
```

### `POST /v1/reports`
Queue a report. Returns **202 immediately** — generation takes 3–6 minutes.

`report_type` is always required; the remaining fields depend on the type.

**Single-team report**
```bash
curl -sS -X POST https://HOST/v1/reports \
  -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"report_type":"team","team_short":"Georgia",
       "team_full":"Georgia Bulldogs","year":2025}'
```

**Matchup report**
```bash
curl -sS -X POST https://HOST/v1/reports \
  -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"report_type":"matchup",
       "home_full":"Georgia Bulldogs","away_full":"Marshall Thundering Herd",
       "home_short":"Georgia","away_short":"Marshall","year":2025}'
```

```json
{"job_id":"9fea9ea3ffa4","state":"queued","percent":0,
 "report_type":"team","subject":"Georgia",
 "message":"Report generation started. Poll /v1/reports/{job_id} for progress."}
```

> `team_short` / `home_short` / `away_short` must be the **CFBD school name** (`Georgia`, `Miami`, `Ohio State`) — that is the key CollegeFootballData indexes on. `*_full` is display text only.

Re-posting the same subject while a build is in flight returns the **existing** `job_id` instead of starting a second one.

### `GET /v1/reports/{job_id}`
Poll every ~4 seconds.

```json
{"job_id":"9fea9ea3ffa4","state":"running","percent":60,
 "message":"Rendering charts","elapsed_seconds":94,
 "report_type":"team","subject":"Georgia","report_ready":false}
```

`state` is `queued` → `running` → `done` | `error`.

On completion:
```json
{"state":"done","percent":100,"report_ready":true,
 "download_url":"/v1/reports/9fea9ea3ffa4/download",
 "result":{"filename":"team_Georgia_August 1, 2026.pdf","seconds":214,
           "sources":23,"sections_with_research":6,
           "games_played":8,"games_upcoming":4}}
```

On failure, `error` carries the summary and `detail` the actionable specifics.

### `GET /v1/reports/{job_id}/download`
Returns the PDF (`application/pdf`). `409` if not finished, `410` if the file has been swept.

### `GET /v1/reports`
Jobs your account has run during the current service lifetime (in-memory; cleared on restart).

---

## Account self-service

### `GET /v1/account`
```json
{"id":1,"account_name":"CFBReports.com","api_key_prefix":"cfbr_ZDw6zS",
 "active":true,"allowed_reports":["matchup","team"],
 "has_custom_watermark":true,
 "settings":{"search_max_results":9},
 "effective_settings":{"research_model":"deepseek/deepseek-v4-flash",
                       "report_model":"moonshotai/kimi-k3",
                       "search_engine":"exa","search_max_results":9,
                       "research_effort":"medium","report_effort":"high",
                       "research_max_tokens":8000,"report_max_tokens":96000}}
```

`settings` = your overrides. `effective_settings` = service defaults with your overrides applied — this is what actually runs.

### `GET /v1/account/usage`
Call counts and recent history for this key.

```json
{"account_id":1,"total_requests":42,"completed":39,"failed":2,"in_progress":1,
 "last_30_days":18,"last_used":"2026-08-01T14:22:09",
 "by_report_type":{"team":30,"matchup":12},
 "recent":[{"report_type":"team","subject":"Georgia","state":"done",
            "seconds":214,"sources":23,"created_at":"2026-08-01T14:18:35"}]}
```

`state` is `queued`, `done`, `error`, or `duplicate` — the last meaning a build for that
subject was already running, so the request was counted but no second build started.

### `PATCH /v1/account/settings`
Only the keys you send change. Send a key as `null` to drop the override and fall back to the default.

```bash
curl -sS -X PATCH https://HOST/v1/account/settings \
  -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"research_model":"deepseek/deepseek-v4-flash-0731",
       "report_model":"moonshotai/kimi-k3",
       "search_max_results":10,"report_effort":"high"}'
```

| Setting | Type | Default | Notes |
|---|---|---|---|
| `research_model` | model id | `deepseek/deepseek-v4-flash` | Runs the live web searches. Must include the provider prefix. |
| `report_model` | model id | `moonshotai/kimi-k3` | Writes the finished report. |
| `search_engine` | `exa` \| `native` \| `""` | `exa` | **`native` only works for OpenAI/Anthropic/Google/xAI models.** DeepSeek has no native search — leave this on `exa`. |
| `search_max_results` | 1–25 | `5` | Exa bills ~$4/1,000 results. 5 ≈ $0.02 per research call. |
| `research_effort` | `low` \| `medium` \| `high` | `medium` | |
| `report_effort` | `low` \| `medium` \| `high` | `high` | |
| `research_max_tokens` | 1000–200000 | `8000` | |
| `watermark_opacity` | 0.01-1.0. How strongly the watermark is stamped. The default 0.09 suits a solid, full-contrast image — the renderer does the fading. Raise it toward 1.0 for an image that is already faint |
| `watermark_scale` | 0.1-1.0. Fraction of the page the mark spans |
| `report_max_tokens` | 2000–400000 | `96000` | Reasoning models bill thinking against this. Too low → empty output. |

Invalid values are rejected with `400` and an explanation; nothing is silently ignored.

### Watermarks

Every report is stamped on every page. Without an upload you get the service default.

**`POST /v1/account/watermark`** — multipart or base64, whichever suits your client.

```bash
# multipart
curl -sS -X POST https://HOST/v1/account/watermark \
  -H "X-Api-Key: $KEY" -F "file=@logo.png"

# base64 JSON
curl -sS -X POST https://HOST/v1/account/watermark \
  -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d "{\"image_base64\":\"$(base64 -w0 logo.png)\",\"content_type\":\"image/png\"}"
```

PNG, JPEG or WebP; 5 MB max. The image is decoded and verified on upload, so a corrupt file is rejected at `400` rather than breaking a PDF three minutes into a build. A transparent-background PNG works best — it is composited at ~9% opacity, centred, scaled to fill the page.

**`GET /v1/account/watermark`** → metadata. Add `?download=1` for the image itself.
**`DELETE /v1/account/watermark`** → revert to the service default.

---

## Administration

Requires the bootstrap `ADMIN_API_KEY` (from `/etc/afplna.env`) or an account with `is_admin: true`.

### `POST /v1/admin/accounts`
```bash
curl -sS -X POST https://HOST/v1/admin/accounts \
  -H "X-Api-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"account_name":"CFBReports.com",
       "contact_email":"ops@cfbreports.com",
       "allowed_reports":["team","matchup"],
       "settings":{"search_max_results":10}}'
```
```json
{"message":"Account created. Store the api_key now — it cannot be retrieved again.",
 "api_key":"cfbr_ZDw6zSj_RmQ...","account":{"id":1,"...":"..."}}
```

`allowed_reports` defaults to `["matchup","team"]`. `is_admin: true` grants admin rights to the new account.

### Other admin endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/admin/accounts` | List all accounts |
| `GET` | `/v1/admin/accounts/{id}` | One account, with settings |
| `PATCH` | `/v1/admin/accounts/{id}` | Change `account_name`, `contact_email`, `active`, `is_admin`, `allowed_reports`, `settings` |
| `POST` | `/v1/admin/accounts/{id}/rotate-key` | New key; the old one dies immediately |
| `DELETE` | `/v1/admin/accounts/{id}` | Deactivate (soft — history is preserved) |

```bash
# grant a new report type
curl -sS -X PATCH https://HOST/v1/admin/accounts/2 \
  -H "X-Api-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"allowed_reports":["team","matchup","conference"]}'
```

---

## Report types

### `team` — Single-Team Season Report
Sections, in order: **Overall Outlook · Schedule and Game-by-Game Breakdown · Practice Notes · Roster News · Injury Report · What the Media Is Saying · What the Coaches Are Saying**

Six parallel live-web research calls (schedule, practice, roster, injuries, media, coaches) feed the news sections; every number comes from CollegeFootballData. Four charts: season results by margin, efficiency percentile radar, PPA form trend, top player impact.

### `matchup` — Head-to-Head Matchup Report
Twenty sections and eight charts, with a projected final score anchored to a deterministic SP+/FPI/Elo baseline blended with the market line. Eight parallel research calls.

### Planned
`conference` (conference-wide) and `injury` (league-wide sweep) are reserved names. Entitlements can be granted ahead of the build; requesting one before it ships returns `400`.

---

# Part 2 — Legacy API (unchanged)

Authenticates with the single `SERVICE_API_KEY`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/generate-report` | Queue a matchup report → `202`. `?wait=true` blocks and returns `200`. |
| `GET` | `/report-status?home_team=&away_team=` | Progress |
| `GET` | `/get-report?home_team=&away_team=` | Download the PDF |
| `GET` | `/has-report?home_team=&away_team=` | `{exists: bool}` |

---

# Health

| Path | Checks |
|---|---|
| `GET /health` | CFBD key, OpenRouter + both models, accounts table, Rotowire DB, wkhtmltopdf, charts, reports and watermarks dirs |
| `GET /health/cfbd` | Probes every CFBD endpoint individually, sequentially |
| `GET /health/llm` | Pings both models |
| `GET /ping` | Liveness |

```bash
curl -sS "https://HOST/health?api_key=$SERVICE_API_KEY" | jq
```

**Debugging tip:** don't pipe through `jq` while diagnosing — it hides the response. Use `curl -sS -i` first. `502` = Gunicorn down (import error), `404` = route not deployed, `401` = key rejected.

---

# Admin console (SSH)

Everything below can also be driven from a terminal console on the droplet, which is
usually faster than curling admin endpoints:

```bash
cd /opt/afplna && ./venv/bin/python admin_tui.py
```

It manages accounts and keys, service-wide and per-account settings, and can audit and
repair the database schema. See the README for the screen and key reference.

# Setup

```bash
# /etc/afplna.env
ADMIN_API_KEY='choose-a-long-random-string'
```

```bash
sudo systemctl restart afplna

# the accounts table is created automatically on first use; confirm:
curl -sS "https://HOST/health?api_key=$SERVICE_API_KEY" | jq .checks.accounts

# mint the first account
curl -sS -X POST https://HOST/v1/admin/accounts \
  -H "X-Api-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"account_name":"CFBReports.com","allowed_reports":["team","matchup"]}'
```

---

# End-to-end example

```bash
KEY=cfbr_your_key_here
HOST=https://your-host

JOB=$(curl -sS -X POST $HOST/v1/reports -H "X-Api-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"report_type":"team","team_short":"Georgia","year":2025}' | jq -r .job_id)

while true; do
  S=$(curl -sS "$HOST/v1/reports/$JOB" -H "X-Api-Key: $KEY")
  echo "$S" | jq -r '"\(.percent)% \(.message)"'
  ST=$(echo "$S" | jq -r .state)
  [ "$ST" = "done" ] && break
  [ "$ST" = "error" ] && { echo "$S" | jq -r '.error, .detail'; exit 1; }
  sleep 4
done

curl -sS -o report.pdf "$HOST/v1/reports/$JOB/download" -H "X-Api-Key: $KEY"
```

---

# Cost per report

| Component | Approx. |
|---|---|
| Research (DeepSeek V4 Flash, $0.14/$0.28 per M) | ~$0.01 |
| Web search (Exa, $4/1,000 results @ 5 × 6–8 calls) | $0.12–0.16 |
| Synthesis (Kimi K3, $3/$15 per M) | ~$0.40 |
| **Total** | **~$0.55** |

`search_max_results` is the main lever: doubling it roughly doubles search spend.
