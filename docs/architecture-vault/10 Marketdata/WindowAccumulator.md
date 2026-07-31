# WindowAccumulator

**Anchor:** `aggregate/window.py:83`

**Purpose:** Decayed weighted-sum accumulator and blended_sentiment - the ONLY point FinBERT and Loughran-McDonald are combined (I7), kind-aware by text_kind.

**Receives from:** [[score_clusters]] via [[cluster_scores]] — accumulates scored clusters with time decay.

**Feeds:** [[SignalEngine.build_window|build_window]] — the signal window is a decayed weighted sum.

**Feeds:** [[build_evidence_bundle]] — the agents see the same decayed evidence.

*Stage: 10 Marketdata*
