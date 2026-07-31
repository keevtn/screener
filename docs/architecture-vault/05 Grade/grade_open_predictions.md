# grade_open_predictions

**Anchor:** `grade/job.py:25`

**Purpose:** Nightly idempotent driver that grades every open prediction (real + shadow) through the identical Grader path.

**Receives from:** [[run_cycle]] via [[predictions]] — walks the open ledger each heavy sweep.

**Feeds:** [[Grader.grade|grade]] — invokes the grading rule per open prediction.

*Stage: 05 Grade*
