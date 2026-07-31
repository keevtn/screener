# SignalEngine.evaluate

**Anchor:** `signal/engine.py:116`

**Purpose:** Per-ticker gate: a 24h open-prediction cooldown per (ticker, config) then writes one frozen prediction (horizon=3d, threshold=0.02, evidence_json={cluster_ids}, status=open).

**Receives from:** [[run_signal_cycle]] — invoked per candidate ticker.

**Receives from:** [[evaluate_window]] — only writes when the window clears the abstain band.

**Receives from:** [[SignalEngine.evaluate|evaluate]] via [[predictions]] — reads recent open predictions for the cooldown.

**Feeds:** [[SignalEngine.evaluate|evaluate]] via [[predictions]] — writes the frozen open prediction.

**Feeds:** [[grade_open_predictions]] via [[predictions]] — the open ledger the grader later matures.

**Feeds:** [[emit_baselines]] via [[predictions]] — each real prediction seeds three shadows.

*Stage: 04 Signal*
