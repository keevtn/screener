# PaperAccountReader

**Anchor:** `marketdata/paper_account.py:65`

**Purpose:** GET-only Alpaca account reader (non-paper host raises; no order method exists) with a ~10s TTL cache, serving the 12 /trader/* endpoints.

**Receives from:** [[AlpacaPaperBroker]] via [[sim_trades]] — reflects positions the broker opened.

**Feeds:** [[api_app]] — serves account state to /trader/*.

*Stage: 07 Trader*
