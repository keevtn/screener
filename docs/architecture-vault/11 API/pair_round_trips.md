# pair_round_trips

**Anchor:** `api/trader.py:199`

**Purpose:** Pure signed-position FIFO lot matcher that pairs Alpaca fills into round trips for the blotter.

**Receives from:** [[AlpacaPaperBroker]] via [[sim_trades]] — matches the broker fills.

**Feeds:** [[provenance_join]] via [[sim_trades]] — round trips feed the provenance join.

**Feeds:** [[api_app]] via [[sim_trades]] — served on /trader/blotter.

*Stage: 11 API*
