# _persist_and_resolve

**Anchor:** `enrich/backfill.py:140`

**Purpose:** Idempotently rebuilds attributions for the touched clusters: deletes+rewrites cluster_entities (ticker, role, match_method) and unmapped_mentions.

**Receives from:** [[EntityResolver.resolve_cluster|resolve_cluster]] — consumes the resolve result per cluster.

**Feeds:** [[build_attention_daily]] via [[cluster_entities]] — the ticker attributions the rollup groups by.

**Feeds:** [[SignalEngine.build_window|build_window]] via [[cluster_entities]] — maps a scored cluster to the ticker the signal is about.

**Feeds:** [[_persist_and_resolve]] via [[unmapped_mentions]] — records declined mentions for the Gate-2 unmapped-rate metric.

*Stage: 02 Enrich*
