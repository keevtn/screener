# mark_observations

**Anchor:** `lab/marking.py`

**Purpose:** Marks matured lab observations with their cumulative abnormal return, bounded per sweep by MARK_BUDGET (spine step 11).

**Receives from:** [[observe_scored_clusters]] via [[signal_observations]] — marks the oldest open observations.

**Receives from:** [[MarketDataProvider]] — needs bars to compute the CAR.

**Feeds:** [[lab_analysis]] via [[signal_observations]] — supplies matured marks.

*Stage: 05 Grade*
