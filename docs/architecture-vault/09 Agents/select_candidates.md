# select_candidates

**Anchor:** `agents/candidates.py:95`

**Purpose:** Selects the agent candidate set: high_alert union extreme-sentiment clusters, capped at 50, so the LLM only ever sees pre-filtered material events.

**Receives from:** [[score_clusters]] via [[cluster_scores]] — picks only high-alert/extreme clusters to bound cost.

**Feeds:** [[build_evidence_bundle]] — hands the candidate clusters to the bundler.

*Stage: 09 Agents*
