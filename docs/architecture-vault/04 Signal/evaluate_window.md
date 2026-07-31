# evaluate_window

**Anchor:** `signal/engine.py:39`

**Purpose:** Pure threshold rules on the WindowState: abstain if item_count<2 or |sentiment_composite|<0.15, else direction=sign(s) with confidence = min(1, 0.5 + 0.5*min(1,(|s|-thr)/thr) + 0.1*min(1,materiality)).

**Receives from:** [[SignalEngine.build_window|build_window]] — consumes the composite window because direction is the sign of net sentiment.

**Feeds:** [[SignalEngine.evaluate|evaluate]] — returns a PredictionIn or None.

*Stage: 04 Signal*
