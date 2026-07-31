# metrics_by_config

**Anchor:** `grade/metrics.py:83`

**Purpose:** Aggregates graded predictions per config into hit_rate, coverage, precision/recall (2x2) and mean lead time for the /metrics endpoint and the EVAL view.

**Receives from:** [[apply_grade]] via [[predictions]] — reads graded outcomes because metrics need resolved rows.

**Feeds:** [[api_app]] — serves the config scoreboard to /metrics.

*Stage: 05 Grade*
