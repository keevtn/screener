# cluster_scores

*Table / hub node.*

**Holds:** One score row per cluster: finbert_label/score, lm_score, catalyst_type, materiality, direction_hint, high_alert, predictive, reaction_dependent, text_kind.

**Written by:** [[score_clusters]]

**Read by:** [[SignalEngine.build_window|build_window]], [[arm_reaction_dependent]], [[build_attention_daily]], [[evaluate_entries]], [[premarket_panel]], [[select_candidates]], [[WindowAccumulator]], [[observe_scored_clusters]]

*Stage: 12 Tables*
