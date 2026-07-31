# vol.atr_fraction

**Anchor:** `marketdata/vol.py:10`

**Purpose:** Computes the ATR fraction used to size volatility-based stops.

**Receives from:** entry point — external feed.

**Feeds:** [[evaluate_entries]] — snapshots entry ATR.

**Feeds:** [[decide_exit]] — vol_stop triggers off it.

*Stage: 10 Marketdata*
