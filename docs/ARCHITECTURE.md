# Architecture

## Data flow

```
ingest → raw_items → enrich → clusters → score → signal → observe → grade
                        │                    │                 │
                        └───────── read-only FastAPI ──────────┘
                                        │
                                   Next.js UI
```

1. **Ingest** — aiohttp/httpx extractors pull RSS, SEC EDGAR, FDA, and social
   (Reddit/Bluesky) items. Each lands once in `raw_items` (append-only). Dedup is by
   content, so a re-seen item is a cheap no-op.
2. **Enrich** — near-duplicate headlines are clustered; the origin item is tagged
   with tickers (alias + name matching against the seeded entity table).
3. **Score** — each **cluster** (not each item) gets a sentiment score (FinBERT *or* a
   Loughran-McDonald lexicon), a catalyst type (M&A / earnings / FDA / guidance / …),
   an event stage, and a materiality weight.
4. **Signal / observe / grade** — scored clusters feed the screener signal and
   pre-registered observations; predictions are issued and later **graded on their
   real horizon** — never early, so there is no lookahead.
5. **Serve** — a read-only FastAPI app reads the same SQLite DB and the Next.js
   dashboard renders it. An optional LLM agent layer (ranker/analyst) *proposes* a
   cited watchlist into its own tables; it never writes to configs or the ledger.

A **two-speed loop** (`scripts/run_pipeline.py`) runs the fast deterministic sweep
(ingest → enrich → score → signal) every ~2 min and the full sweep (baselines,
grading, attention/buzz rollup) on a slower cadence. Every step is idempotent.

## Storage

One **SQLite** database (`DATABASE_URL`), opened WAL-mode with a busy timeout so the
pipeline writer and the API reader/occasional-writer share the file. The schema is
deliberately SQLite-specific — append-only `CREATE TRIGGER`s and dialect upserts
enforce the invariants below — which is why the deployment mounts a **volume** and
colocates the worker with the API rather than porting to Postgres.

## The LIVE feed (deploy change)

Upstream, the news tape read from an external Mongo archive via a separate
middleware. This build **drops that dependency**: the API serves the LIVE feed
straight from `raw_items` at `GET /api/news` (newest-first, cluster-attributed with
tickers + sentiment, in the frontend's `NewsItem` shape). One service, one database,
no external datastore. SSE fan-out (`/events`) runs in-process; `REDIS_URL` is only
needed to fan out across multiple instances.

## Load-bearing invariants

Enforced in code (see `src/pipeline/common/models.py` and the module comments):

- **I1 — time is always UTC-aware.** Naive datetimes are rejected at bind time;
  `now()` is confined to `common/timeutil.py`.
- **I2 — `raw_items` is append-only.** ORM hooks raise on update/delete and SQLite
  triggers back them up. History is immutable.
- **I3 — configs are immutable once created.** Changes create a new version; the UI
  approves/rejects proposals rather than mutating in place.
- **I4 — predictions are immutable after issue**, except the grader's outcome fields.
- **I6 — the signal spine never imports the LLM.** The agent client is an injectable
  seam, so the deterministic path is testable without the network and can't be
  perturbed by model output.
- **I9 — secrets come from the environment only.** Nothing credential-shaped is
  committed; see [`ENVIRONMENT.md`](ENVIRONMENT.md).
- **I12 — grading matures on the real horizon.** The fast loop refreshes scoring and
  signals, never outcomes — no early/lookahead grading.

When you change behavior one of these describes, update this note in the same change.
