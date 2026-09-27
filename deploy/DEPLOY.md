# Deploying Vera for the magicpin AI Challenge

The judge drives your bot over HTTP at a **public URL** you submit via the
submission portal (`https://<your-host>/v1/*`). It expects all 5 endpoints
(`healthz`, `metadata`, `context`, `tick`, `reply`, plus optional `teardown`),
in-memory state that survives across calls, and **no restarts during the test**.
A bot offline 3× consecutive healthz polls is scored −10 (operational); each
tick/reply timeout over 30s costs −1.

## 0. Before you deploy (local)

```bash
py -X utf8 -m pytest -q                       # 325 passed
py -X utf8 tools\verify_codex.ps1             # full 6-step gate, exit 0
py -X utf8 -m uvicorn app:app --port 8080     # serve locally
set BOT_URL=http://localhost:8080 && py -X utf8 judge_simulator.py   # mock judge
```

## 1. Environment variables (required — `/v1/metadata` reports them)

| Variable | Example | Notes |
|---|---|---|
| `VERA_TEAM_NAME` | `Dhoni_Dreamers` | exactly as registered |
| `VERA_TEAM_MEMBERS` | `Shashank,Aditi` | comma-separated |
| `VERA_CONTACT_EMAIL` | `you@example.com` | judge contact; never ship the placeholder |
| `VERA_VERSION` | `1.0.0` | bump per resubmission |
| `VERA_SUBMITTED_AT` | `2026-09-28T09:00:00Z` | set when you submit |
| `VERA_APPROACH` | one-liner | shown in metadata |
| `VERA_STATE_DIR` | `/data` (Fly volume) or default | journal + conversations |
| `VERA_LLM_PROVIDER` / `VERA_LLM_MODEL` / `VERA_LLM_API_KEY` | `gemini` / `gemini-2.5-flash` / key | **optional** — without them the bot runs the deterministic floor, which is fully scoreable |

Never leave `contact_email: not-provided@example.invalid` or
`submitted_at: "not submitted"` in the deployed metadata — the judge reads
them first.

## 2. Option A — Fly.io (recommended: Dockerfile ships, ~$3/mo, no sleep)

```bash
fly auth login
fly launch --no-deploy --now          # detects deploy/Dockerfile; internal_port 8080
fly volumes create vera_state --size 1 --region bom
fly secrets set VERA_TEAM_NAME=... VERA_TEAM_MEMBERS=... VERA_CONTACT_EMAIL=...
fly secrets set VERA_VERSION=1.0.0 VERA_SUBMITTED_AT=2026-09-28T09:00:00Z
fly deploy
```

Then in `fly.toml` **keep the machine always on** (in-memory conversation
state + the judge polls repeatedly):

```toml
[http_service]
  internal_port = 8080
  force_https = true
  [http_service.checks]
    interval = "15s"
    path = "/v1/healthz"

[vm]
  min_machines_running = 1   # never auto-stop; a sleeping bot = missed ticks
```

Mount the volume at `/data` and set `VERA_STATE_DIR=/data`. Verify:
`curl https://<app>.fly.dev/v1/healthz` → `{"status":"ok",...}`.

## 3. Option B — Railway / Render

**This repo ships a Render blueprint (`render.yaml` at the root).** Fastest
path: Render dashboard → New → **Blueprint** → select your GitHub repo →
apply. It creates the Docker web service with health check `/v1/healthz`
and prompts for the secret values (`VERA_LLM_API_KEY`, team identity fields).

Manual setup, if you prefer:

1. New service → Deploy from repo → **Docker** (uses `deploy/Dockerfile`,
   which runs `deploy/run.sh`: `uvicorn app:app --workers 1 --timeout-keep-alive 75`).
2. Set the env vars from the table above in the dashboard.
3. Attach a persistent disk mounted at `/data` (`VERA_STATE_DIR=/data`).
4. **Disable scale-to-zero / use a paid always-on instance.** Render's free
   tier sleeps after 15 min idle — a sleeping bot misses healthz polls (−10)
   and ticks (−1 each). This is the #1 way submissions die.
5. Health check path: `/v1/healthz`, port 8080.

## 4. Option C — ₹500 VPS + Caddy (STRATEGY.md's budget pick)

```bash
# on the VPS (Ubuntu 24.04, DNS A record -> your box)
git clone <repo> /opt/vera && cd /opt/vera
docker build -f deploy/Dockerfile -t vera . 
docker run -d --name vera --restart unless-stopped \
  -p 127.0.0.1:8080:8080 -v /opt/vera_state:/var/lib/vera \
  -e VERA_TEAM_NAME=... -e VERA_CONTACT_EMAIL=... vera
```

Caddyfile (automatic HTTPS, no cert fiddling):

``    your-domain.com {
        reverse_proxy 127.0.0.1:8080
    }``

Systemd/Caddy auto-starts on boot; `--restart unless-stopped` covers crashes.

## 5. Why not ngrok

STRATEGY.md is explicit: ngrok is fine for development, **not acceptable for
the real run** — the URL changes on restart, tunnels drop, and the judge
re-polls `/v1/healthz` for the whole test window.

## 6. Pre-flight checklist (challenge-testing-brief.md §12)

- [ ] `curl https://<host>/v1/healthz` returns 200 from a machine **outside** your network
- [ ] `/v1/metadata` shows real team name, members, contact email, submitted_at
- [ ] `/v1/context` idempotent on `(scope, context_id, version)` — same version replay → 409 `stale_version`
- [ ] `/v1/tick` returns `{"actions": []}` within budget even when idle (our cap: ~7.4s compose wait, well under 30s)
- [ ] `/v1/reply` answers any conversation (unknown/empty payloads degrade to `action: end`)
- [ ] State persists across calls in one process: single uvicorn worker (`deploy/run.sh` enforces `--workers 1`)
- [ ] `judge_simulator.py` against the deployed URL scores non-zero
- [ ] URL submitted via the portal; `submission.jsonl` (30 lines) + `README.md` attached
