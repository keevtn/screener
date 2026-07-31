# score_clusters

**Anchor:** `score/score.py:214`

**Purpose:** The scorer: walks unscored clusters newest-first (cap FINBERT_MAX_PER_SWEEP|48), FinBERT+LM sub-batched at 8, each cluster in a savepoint with a sentiment-only fallback, and writes one cluster_scores row per cluster (I5).

**Receives from:** [[persist_clusters]] via [[clusters]] — walks clusters that lack a score row because scoring is incremental.

**Receives from:** [[scorer_visible_stmt]] via [[raw_items]] — reads origin text only for non-social clusters (I8).

**Receives from:** [[CatalystTaxonomy.classify|classify]] — gets the catalyst axis per cluster.

**Receives from:** [[resolve_finbert]] — gets the resolved sentiment analyzer.

**Receives from:** [[text_kind_of]] — gets the text kind for sentiment weighting.

**Feeds:** [[SignalEngine.build_window|build_window]] via [[cluster_scores]] — the catalyst+sentiment rows the signal window is built from.

**Feeds:** [[build_attention_daily]] via [[cluster_scores]] — supplies finbert_score for the sentiment rollup.

**Feeds:** [[evaluate_entries]] via [[cluster_scores]] — the fresh scored features the paper trader matches on.

**Feeds:** [[premarket_panel]] via [[cluster_scores]] — the panel ranks off today's scored clusters.

**Feeds:** [[select_candidates]] via [[cluster_scores]] — agents pick high-alert / extreme-sentiment clusters.

**Feeds:** [[score_clusters]] via [[score_status]] — writes a live breadcrumb read by /health.

*Stage: 03 Score*
