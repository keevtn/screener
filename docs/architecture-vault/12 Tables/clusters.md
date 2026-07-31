# clusters

*Table / hub node.*

**Holds:** One row per deduped story; cluster_id = origin item id, with member_ids_json and origin_tier.

**Written by:** [[persist_clusters]]

**Read by:** [[score_clusters]], [[EntityResolver.resolve_cluster|resolve_cluster]], [[build_attention_daily]], [[SignalEngine.build_window|build_window]], [[provenance_join]]

*Stage: 12 Tables*
