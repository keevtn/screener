# _step

**Anchor:** `run_pipeline.py:78`

**Purpose:** Wraps each pipeline step: times it, logs the result, swallows exceptions, and rolls back the shared session so one dead step cannot poison the rest of the sweep.

**Receives from:** [[run_cycle]] — invoked for every one of the 16 steps.

*Stage: 00 Spine*
