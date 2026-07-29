# Environment reference

Every variable the stack reads, with its default. **The whole thing boots with no
`.env` at all** — each key has a safe default or degrades to a no-op, so you only
add what you want to switch on. Copy the block at the bottom to `.env` for local
dev (`.env` is gitignored — never commit real secrets). On Railway, set these as
**service variables** instead of a file.

## Which service gets what

| Scope | Variables |
|-------|-----------|
| **App service** (API + pipeline) | `DATABASE_URL`, `PORT`/`HOST` (injected), `PIPELINE_INTERVAL`, the LLM / market-data / ingestion keys below |
| **Frontend service** | `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_PREDICTION_API_URL` — **baked at build time**, so they must be set before the frontend build runs |

## Storage

| Var | Default | Notes |
|-----|---------|-------|
| `DATABASE_URL` | `sqlite:///data/pipeline.db` | SQLAlchemy URL for the one SQLite DB shared by the pipeline loop and the API. On Railway point at the mounted volume with **four** slashes (absolute): `sqlite:////data/pipeline.db`. |

## Web process (app service)

| Var | Default | Notes |
|-----|---------|-------|
| `PORT` | `8001` | Injected by Railway; the API binds it. |
| `HOST` | `127.0.0.1` | The Railway start script sets `0.0.0.0`. |
| `PIPELINE_INTERVAL` | `300` | Full-sweep cadence (s); the fast sweep is a fraction of it. |

## Frontend (NEXT_PUBLIC_* — build-time)

| Var | Default | Notes |
|-----|---------|-------|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8001` | News/screener/predictions API. In deploy → the app service's public URL. |
| `NEXT_PUBLIC_PREDICTION_API_URL` | `http://localhost:8001` | Same app service here (the two APIs are collapsed into one). |

## LLM agent layer (ranker + analyst) — optional

The signal spine never calls the LLM, so leaving these blank just disables the
RANK / deep-dive panels.

| Var | Default | Notes |
|-----|---------|-------|
| `CLAUDE_API` | `true` | `true` → metered Anthropic API (needs `ANTHROPIC_API_KEY`); `false` → Claude subscription via the Agent SDK (needs `CLAUDE_CODE_OAUTH_TOKEN`). |
| `ANTHROPIC_API_KEY` | — | Metered API key. |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | From `claude setup-token`, for subscription mode. |
| `AGENT_DAILY_USD_CAP` | `2.0` | Soft USD/day cap before a run refuses to start. |
| `AGENT_RANKER_CANDIDATES` | `40` | Candidate breadth per ranking run. |

## Market data + sim — optional

| Var | Default | Notes |
|-----|---------|-------|
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | — | Price bars + paper-trading sim. Absent → sim/live quotes inert; news + screener still work. |
| `SIM_ENABLED` | `false` | Arm the paper-trading simulation loop. |
| `FINVIZ_AUTH_TOKEN` | — | Primary universe/fundamentals provider; absent → free Nasdaq directory fallback. |

## Ingestion identity + social — optional but polite

| Var | Default | Notes |
|-----|---------|-------|
| `SEC_CONTACT_EMAIL` / `EDGAR_USER_AGENT` | — | SEC EDGAR wants a contact string in the User-Agent. |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USER_AGENT` | — | Reddit social lane. |
| `SEARCH_TRENDS_ENABLED` | `false` | Google-trends attention lane. |

## Fan-out / alerts — optional (a single instance needs none)

| Var | Default | Notes |
|-----|---------|-------|
| `REDIS_URL` / `REDIS_URI` | — | Cross-instance SSE fan-out. Unset → in-process event stream (correct for one instance). |
| `ALERT_WEBHOOK_URL` | — | POST high-alert catalysts to a webhook. |

---

## Copy-paste template

```dotenv
# Storage — on Railway use sqlite:////data/pipeline.db (four slashes, on the volume)
DATABASE_URL=sqlite:///data/pipeline.db

# Web process (Railway injects PORT and sets HOST=0.0.0.0; leave unset locally)
# PORT=8001
# HOST=0.0.0.0
# PIPELINE_INTERVAL=300

# Frontend (set on the FRONTEND service — baked at build time)
NEXT_PUBLIC_API_URL=http://localhost:8001
NEXT_PUBLIC_PREDICTION_API_URL=http://localhost:8001

# LLM agent layer — optional
# CLAUDE_API=true
# ANTHROPIC_API_KEY=sk-ant-...
# CLAUDE_CODE_OAUTH_TOKEN=
# AGENT_DAILY_USD_CAP=2.0
# AGENT_RANKER_CANDIDATES=40

# Market data + sim — optional
# ALPACA_API_KEY=
# ALPACA_API_SECRET=
# SIM_ENABLED=false
# FINVIZ_AUTH_TOKEN=

# Ingestion identity + social — optional
# SEC_CONTACT_EMAIL=you@example.com
# EDGAR_USER_AGENT=screener/1.0 (you@example.com)
# REDDIT_CLIENT_ID=
# REDDIT_CLIENT_SECRET=
# REDDIT_USER_AGENT=screener/1.0 by u/you
# SEARCH_TRENDS_ENABLED=false

# Fan-out / alerts — optional (single instance needs none)
# REDIS_URL=
# ALERT_WEBHOOK_URL=
```
