# persist_clusters

**Anchor:** `enrich/cluster.py:158`

**Purpose:** Upserts cluster rows on cluster_id (created_at preserved on merge), writing member_ids_json, origin_item_id, origin_tier and member_count.

**Receives from:** [[build_clusters]] — receives the ClusterResults to persist.

**Feeds:** [[score_clusters]] via [[clusters]] — the durable cluster rows the scorer walks.

**Feeds:** [[EntityResolver.resolve_cluster|resolve_cluster]] via [[clusters]] — provides the origin item to resolve tickers from.

*Stage: 02 Enrich*
