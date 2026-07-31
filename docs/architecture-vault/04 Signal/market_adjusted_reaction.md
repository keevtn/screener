# market_adjusted_reaction

**Anchor:** `signal/armed.py:91`

**Purpose:** Computes the first strictly-post-event market-adjusted reaction (I12, no lookahead) used to sign an armed prediction.

**Receives from:** [[MarketDataProvider]] — needs benchmark + ticker bars after the event.

**Feeds:** [[resolve_armed_state]] — supplies the signed reaction.

*Stage: 04 Signal*
