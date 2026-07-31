# backfill_enrichment

**Anchor:** `enrich/backfill.py:70`

**Purpose:** Enrichment orchestrator: reads recent raw_items (7-day window incrementally), using clusters.member_ids_json as the 'already enriched' watermark, then clusters then resolves. Cluster-first, resolve-second.

**Receives from:** [[run_cycle]] via [[raw_items]] — consumes new raw_items each sweep because enrichment is append-driven off ingest.

**Feeds:** [[build_clusters]] — hands the new item set to the clusterer.

**Feeds:** [[_persist_and_resolve]] — drives the resolve+persist pass.

*Stage: 02 Enrich*
