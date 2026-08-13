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

## Voice studio

Episodes rendered by a VibeVoice workstation the account owns, rather than by this
service's OpenRouter TTS. The workstation sits behind a home NAT, so this service never
calls it: the workstation polls, claims, renders and posts the audio back. A finished
episode is published through the same path as any other and is indistinguishable
downstream — same table, same filename convention, same RSS feed.

Two credentials are in play. The console uses its **account key**; the workstation uses a
**shared worker token** (`VOICE_WORKER_TOKEN`) plus an `X-Worker-Id` header identifying
the machine. The token is deliberately not an account key: it lives on a desk we do not
administer, so a leak must not expose reports or report-generation spend.

### Console side (account key)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/voice-jobs` | Queue an episode → `201` |
| `GET` | `/v1/voice-jobs` | This account's recent jobs |
| `GET` | `/v1/voice-jobs/{id}` | One job's state and progress |
| `POST` | `/v1/voice-jobs/{id}/cancel` | Abandon a job |
| `GET` | `/v1/voice-studio` | Is a studio online, and what voices does it have |
| `POST` | `/v1/podcasts/script` | Write a two-host script from reports + instructions |
| `GET` | `/v1/openrouter/models` | Text-to-text models, for the script picker |

```bash
curl -X POST https://api.example.com/v1/voice-jobs \
  -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"title":"Week 3 Preview",
       "script":"Speaker 1: Welcome in.\nSpeaker 2: Big slate today.",
       "automation":"cfb_reports_podcast",
       "preset":"podcast",
       "speakers":{"1":"Johnny_Vibe","2":"Ed_Clean_Vibe"}}'
```

`title` becomes both the episode title and its filename. `speakers` maps speaker number
to a voice profile name on the workstation; omit it, or omit individual speakers, and the
studio falls back to whatever defaults are saved there for that automation.

`GET /v1/voice-studio` answers even when no studio has ever checked in — the console
renders an offline badge from it and must not break because the workstation is switched
off:

```json
{"online": true, "busy": false, "worker_id": "kylee-desktop",
 "catalog": {"voices": ["Johnny_Vibe", "Ed_Clean_Vibe"],
             "presets": ["podcast", "comedic"],
             "models": ["vibevoice/VibeVoice-7B"]},
 "last_seen": "2026-08-12 21:40:11", "seconds_ago": 12}
```

`POST /v1/podcasts/script` takes `{instructions, report_filenames[], model, host_a,
host_b, minutes}` and returns a script already in the studio's two-speaker format. The
reports are read from this disk and their text goes straight to the model — it never
travels out to the website and back.

### Workstation side (worker token)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/voice-jobs/next` | Claim the oldest queued job → `200`, or `204` when idle |
| `PATCH` | `/v1/voice-jobs/{id}` | `{stage, percent}` — also extends the lease |
| `POST` | `/v1/voice-jobs/{id}/audio` | Raw MP3/WAV body → publishes the episode |
| `POST` | `/v1/voice-jobs/{id}/fail` | `{error}` |
| `POST` | `/v1/voice-workers/heartbeat` | `{account_id, label, catalog, busy}` |

A claim carries a **lease** (`VOICE_JOB_LEASE_SECONDS`, default 900). Every `PATCH`
renews it. If the workstation is rebooted or crashes mid-render, the lease expires and the
job returns to `queued` for the next poll — nothing was published, because publication
only happens when audio actually arrives.

Verify the queue against the real database after deploying:

```bash
cd /opt/afplna && ./venv/bin/python voice_queue_selftest.py
```

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

## Enabling the voice studio

Only needed if an account renders episodes on its own VibeVoice workstation. Without
`VOICE_WORKER_TOKEN` set, every worker endpoint answers `503` and the console simply shows
the studio as offline — the rest of the service is unaffected.

```bash
# generate a token
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**First check where the service actually gets its environment.** Adding a variable to
`/etc/afplna.env` only works if the unit loads that file:

```bash
systemctl cat afplna | grep -E 'Environment'
```

- An `EnvironmentFile=/etc/afplna.env` line — add the token to that file.
- Only `Environment=NAME=value` lines — the env file is **not** read by the service
  (`envfile.py` still uses it for CLI tools, which is what makes this easy to miss).
  Either add a drop-in that loads it, which is the tidier fix:

  ```bash
  sudo systemctl edit afplna     # then add the two lines below, save
  ```
  ```ini
  [Service]
  EnvironmentFile=-/etc/afplna.env
  ```

  or set the variable inline in the unit alongside the others.

```bash
# /etc/afplna.env
VOICE_WORKER_TOKEN='the-token-you-just-generated'
# optional:
VOICE_JOB_LEASE_SECONDS=900      # how long a studio may go quiet mid-render
VOICE_JOB_MAX_UPLOAD_MB=200      # largest episode a studio may post back
```

Confirm the **running process** has it — the file being right proves nothing:

```bash
curl -sS "https://HOST/health?api_key=$SERVICE_API_KEY" | jq .checks.voice_studio
```

`{"enabled": true, ...}` means the token reached the process. `false` comes with a hint
naming the most common cause.

```bash
cd /opt/afplna && git pull
sudo systemctl restart afplna

# prove the queue works against the real database
./venv/bin/python voice_queue_selftest.py
```

There is no separate migration step: `voice_jobs.ensure_schema()` runs on first use and
the self-test calls it, so the tables appear on their own.

If you would rather create them explicitly, load the environment first — an interactive
shell does not get `/etc/afplna.env` the way systemd does, and without it the command
fails with `Access denied for user '…' (using password: NO)`:

```bash
./venv/bin/python -c "import envfile; envfile.bootstrap(); import voice_jobs; voice_jobs.ensure_schema()"
```

`envfile.bootstrap()` has to come before anything that imports `config`, which resolves
the database settings at import time. The self-test does this for you.

The same token goes into the workstation's `.env` as `CFBR_WORKER_TOKEN`. If nginx fronts
the API, make sure `client_max_body_size` is at least `VOICE_JOB_MAX_UPLOAD_MB` or
episodes will be refused at the proxy before they reach the service.

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
