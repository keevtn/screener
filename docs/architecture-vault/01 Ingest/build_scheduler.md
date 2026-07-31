# build_scheduler

**Anchor:** `ingest/scheduler.py:30`

**Purpose:** APScheduler alternative to the pipeline loop: one interval job per source (sec=300s, rss=600s, fda=300s, reddit=180s) all calling the same dispatch function.

**Feeds:** [[run_source_once]] — the scheduled path calls the identical dispatch as the manual path.

*Stage: 01 Ingest*
