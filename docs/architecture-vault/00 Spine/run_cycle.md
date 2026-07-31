# run_cycle

**Anchor:** `run_pipeline.py:97`

**Purpose:** One pipeline sweep. Runs the 16 ordered steps; STRUCTURED_SOURCES=(rss,sec,fda) always, SOCIAL_ARCHIVE_SOURCES=(bluesky,reddit) only on heavy sweeps. FAST = steps 1,2,3,4,5,7,8,16.

**Receives from:** [[run_pipeline.main|main]] — receives the shared engine/provider/models built once at startup.

**Feeds:** [[run_source_once]] — step 1 dispatches every ingest source.

**Feeds:** [[backfill_enrichment]] — step 2 clusters + resolves new raw_items.

**Feeds:** [[score_clusters]] — step 3 scores unscored clusters.

**Feeds:** [[observe_scored_clusters]] — step 4 snapshots lab observations.

**Feeds:** [[run_signal_cycle]] — step 7 issues predictions.

**Feeds:** [[evaluate_entries]] — step 8 paper-trades when SIM_ENABLED.

**Feeds:** [[emit_baselines]] — step 9 emits shadow predictions (heavy).

**Feeds:** [[grade_open_predictions]] — step 10 grades matured predictions (heavy).

**Feeds:** [[mark_observations]] — step 11 marks CAR outcomes (heavy).

**Feeds:** [[build_attention_daily]] — step 15 rebuilds attention rollup (heavy).

**Feeds:** [[backfill_prediction_context]] — step 16 carries origin-news context onto predictions.

*Stage: 00 Spine*
