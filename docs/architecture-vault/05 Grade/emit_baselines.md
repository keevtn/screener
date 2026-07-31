# emit_baselines

**Anchor:** `grade/baselines.py:91`

**Purpose:** Emits three shadow predictions per real one: always_up (bullish), random (sha256 parity), momentum (5-day trailing adj-close sign, strictly pre-issue, abstains under 6 closes), all copying horizon/threshold and graded identically.

**Receives from:** [[SignalEngine.evaluate|evaluate]] via [[predictions]] — shadows each real prediction to prove the signal beats naive baselines.

**Feeds:** [[emit_baselines]] via [[predictions]] — writes the shadow predictions with evidence{baseline, shadows:real_id}.

*Stage: 05 Grade*
