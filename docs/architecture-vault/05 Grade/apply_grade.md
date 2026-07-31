# apply_grade

**Anchor:** `grade/grader.py:142`

**Purpose:** Writes exactly the grader fields (status, outcome, realized_adjusted_return, graded_at, resolving_close) and nothing else (I4), enforced by ORM hooks + SQL triggers.

**Receives from:** [[Grader.grade|grade]] — receives the outcome to persist.

**Feeds:** [[apply_grade]] via [[predictions]] — stamps the graded fields on the prediction.

**Feeds:** [[metrics_by_config]] via [[predictions]] — the graded ledger metrics aggregate.

*Stage: 05 Grade*
