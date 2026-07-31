# section_cohorts

**Anchor:** `compute_deck_stats.py:178`

**Purpose:** Freezes the pre-registered evaluation cohorts (n=157/237/109/90/1,065) so gate verdicts are computed on a fixed sample, not a moving target.

**Receives from:** [[metrics_by_config]] via [[predictions]] — cohorts are drawn from the graded ledger.

**Feeds:** [[gates_verdicts]] — supplies the frozen cohorts.

*Stage: 06 Gates*
