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
| `DATABASE_URL` | `sqlite:///data/pipeline.db` | SQLAlchemy URL for the one SQLite DB shared by the pipeline loop and the API. On Railway mount the volume at **`/app/data`** and point here with **four** slashes (absolute): `sqlite:////app/data/pipeline.db`. |
| `RAILWAY_VOLUME_MOUNT_PATH` | *(injected)* | Set by Railway to the volume mount path (`/app/data`). Read-only signal the volume guard cross-checks against where the DB actually lands — you never set this yourself. |
| `SEED_DB_URL` / `SEED_DB_PATH` | `seed/pipeline_seed.db` | Override the source the per-table seed hydrator copies demo history from (see `scripts/hydrate_seed.py`). |

## Web process (app service)

| Var | Default | Notes |
|-----|---------|-------|
| `PORT` | `8001` | Injected by Railway; the API binds it. |
| `HOST` | `127.0.0.1` | The Railway start script sets `0.0.0.0`. |
| `PIPELINE_INTERVAL` | `300` | Full-sweep cadence in seconds (baselines, grading, attention rollup). |
| `PIPELINE_FAST_INTERVAL` | *(fraction of full)* | Fast deterministic sweep cadence (ingest → enrich → score → signal) for freshness. |
| `API_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins for the API. Default `*` (valid because the API sets no credentials); pin to the exact frontend domain(s) to lock down. |

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

## Sentiment scoring — optional

Default is the zero-dependency Loughran–McDonald lexicon. ONNX turns on an
int8-quantized ProsusAI/finbert that runs in-process (no torch/transformers, no
external inference API — the model is a static file downloaded to the volume once
at boot, checksum-verified). Missing model/deps → **automatic lexicon fallback**.

| Var | Default | Notes |
|-----|---------|-------|
| `SENTIMENT_MODE` | `lexicon` | `lexicon` or `onnx`. Surfaced in `/health` → `scoring.sentiment_mode`. |
| `FINBERT_ONNX_URL` | — | Static download URL for `model.int8.onnx` (a GitHub Release asset or HuggingFace resolve URL — *not* an inference API). |
| `FINBERT_ONNX_SHA256` | — | Optional integrity check; on mismatch the file is discarded → lexicon fallback. |
| `FINBERT_ONNX_PATH` | `/app/data/models/finbert-int8.onnx` | Where the model caches on the volume across restarts. |
| `FINBERT_MAX_PER_SWEEP` / `FINBERT_ONNX_THREADS` | — | Advanced tuning: cap FinBERT scores per sweep / ORT thread count. |

## Market data + sim — optional

| Var | Default | Notes |
|-----|---------|-------|
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | — | Price bars + paper-trading sim + the read-only **TRADER** dashboard (`/trader/*`) + the standing driver. **PAPER account keys only.** The web backend is strictly read-only toward Alpaca (account/positions/orders/portfolio-history/clock — no order placement/cancel); order placement happens ONLY in the standing driver's internal clock loop, never behind an HTTP route. Absent → sim/live quotes inert, the TRADER tab shows a "connect Alpaca keys" empty state, and the driver refuses to start; news + screener still work. Keys never reach the browser (all web calls proxied by the API behind a ~10s cache). |
| `SIM_ENABLED` | `false` | Arm the in-pipeline paper-sim step (`run_pipeline`). Does NOT gate the standing driver (that's `TRADER_DRIVER_ENABLED`). |
| `TRADER_DRIVER_ENABLED` | `false` | **Master switch for LIVE paper trading on Railway.** When true, the app service runs the standing daily driver (`scripts/run_trader.py`) alongside the API + pipeline: arm one session per trading day off the Alpaca clock, sweep entries/exits, flatten ~10 min before the close, write the EOD report card. Order placement lives only in this driver's clock loop. Default off = zero behavior change. **Disable trading instantly: unset this and restart the service.** ⚠ **Exactly one driver may trade a paper account** — if Railway trades, the local driver for this account MUST stay off (see README). |
| `TRADER_DRIVER_SWEEP_S` | `60` | Seconds between driver sweeps (entry/exit evaluation). Min 5. |
| `TRADER_VOLUME_GUARD` | `on` | **Emergency operator kill-switch only.** By default the pipeline/driver refuse to run if the DB isn't on a persistent Railway volume (so a mis-mounted deploy can't accumulate data that vanishes on restart). Setting it to `off` **bypasses that persistence check** — intended solely for a deliberate ephemeral/debug run. Leave it on in production. |
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
# Storage — on Railway mount the volume at /app/data and use four slashes (absolute):
#   DATABASE_URL=sqlite:////app/data/pipeline.db
DATABASE_URL=sqlite:///data/pipeline.db
# SEED_DB_URL=            # override the per-table seed source (default seed/pipeline_seed.db)

# Web process (Railway injects PORT and sets HOST=0.0.0.0; leave unset locally)
# PORT=8001
# HOST=0.0.0.0
# PIPELINE_INTERVAL=300
# PIPELINE_FAST_INTERVAL=
# API_CORS_ORIGINS=*      # pin to the frontend domain(s) to lock down

# Sentiment — optional (default lexicon; onnx runs int8 FinBERT in-process)
# SENTIMENT_MODE=lexicon
# FINBERT_ONNX_URL=
# FINBERT_ONNX_SHA256=
# FINBERT_ONNX_PATH=/app/data/models/finbert-int8.onnx

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
# TRADER_DRIVER_ENABLED=false   # master switch for the standing paper driver (Railway)
# TRADER_DRIVER_SWEEP_S=60
# TRADER_VOLUME_GUARD=on        # emergency kill-switch only; leave on in production
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
