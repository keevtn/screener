# run_signal_cycle

**Anchor:** `signal/cycle.py:63`

**Purpose:** Signal step hook: builds the SignalEngine from the immutable config, runs evaluate_all (structured predictions) plus resolve_all_armed (PEAD), and alerts per prediction.

**Receives from:** [[run_cycle]] via [[configs]] — runs with the content-addressed signal params.

**Receives from:** [[SignalEngine.build_window|build_window]] — delegates window construction.

**Receives from:** [[resolve_armed_state]] — appends armed-state resolutions.

**Feeds:** [[SignalEngine.evaluate|evaluate]] — drives per-ticker evaluation.

*Stage: 04 Signal*
