# build_clusters

**Anchor:** `enrich/cluster.py:62`

**Purpose:** Near-dup clustering via rapidfuzz token_set_ratio (cutoff 90) over a rolling 72h window. cluster_id = the origin (earliest-published, tier-tie-broken) item id, so one story is one scoring target (I5).

**Receives from:** [[backfill_enrichment]] via [[raw_items]] — clusters the new items because dedup collapses the same story across wires.

**Feeds:** [[persist_clusters]] — returns new/changed ClusterResults.

*Stage: 02 Enrich*
