# railway_start

**Anchor:** `railway_start.sh`

**Purpose:** Three-phase boot: API-first, then a timed bootstrap (init_db -> hydrate_seed -> assign exit policies -> fetch_model -> seed) then supervised workers with backoff.

**Feeds:** [[hydrate_seed]] — runs the seed hydration phase.

**Feeds:** [[fetch_model]] — runs the model fetch phase.

**Feeds:** [[run_pipeline.main|main]] — supervises the pipeline worker.

*Stage: 13 Ops*
