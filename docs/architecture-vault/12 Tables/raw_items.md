# raw_items

*Table / hub node.*

**Holds:** Content-addressed append-only ingest rows (id=sha256(source|guid|url), source_class, payload_json).

**Written by:** [[RawItemHandler.write|write]]

**Read by:** [[scorer_visible_stmt]], [[backfill_enrichment]], [[build_clusters]], [[EntityResolver.resolve_cluster|resolve_cluster]], [[score_clusters]], [[build_attention_daily]], [[SignalEngine.build_window|build_window]], [[arm_reaction_dependent]], [[live_news]], [[provenance_join]], [[dual_layer_immutability]]

*Stage: 12 Tables*
