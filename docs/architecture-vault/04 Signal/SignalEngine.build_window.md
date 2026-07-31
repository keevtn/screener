# SignalEngine.build_window

**Anchor:** `signal/engine.py:78`

**Purpose:** Builds a ticker's decision window: joins cluster_scores x clusters x cluster_entities x raw_items, enforcing structured-only (I8, source_class != 'social'), predictive, NOT reaction_dependent; blends sentiment and time-decayed cluster weight into a WindowState.

**Receives from:** [[score_clusters]] via [[cluster_scores]] — the window is built only from catalyst-scored predictive events.

**Receives from:** [[_persist_and_resolve]] via [[cluster_entities]] — selects the rows attributed to this ticker.

**Receives from:** [[WindowAccumulator]] — uses the decayed weighted-sum accumulator.

**Feeds:** [[evaluate_window]] — hands the WindowState to the threshold rules.

*Stage: 04 Signal*
