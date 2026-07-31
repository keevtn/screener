# run_pipeline.main

**Anchor:** `run_pipeline.py:390`

**Purpose:** Process entry point: builds engine, tiers, market provider, LM + FinBERT, and the entity resolver, then drives the two-speed loop (FAST sweep ~120s, FULL every --interval).

**Feeds:** [[run_cycle]] — hands its built components to each sweep.

*Stage: 00 Spine*
