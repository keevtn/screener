# observe_scored_clusters

**Anchor:** `lab/observe.py`

**Purpose:** Snapshots scored clusters into lab observations for out-of-sample IC/CAR analysis (spine step 4).

**Receives from:** [[score_clusters]] via [[cluster_scores]] — observes every freshly scored cluster.

**Feeds:** [[mark_observations]] via [[signal_observations]] — the open observations awaiting a CAR mark.

**Feeds:** [[lab_analysis]] via [[signal_observations]] — the sample the lab metrics run over.

*Stage: 05 Grade*
