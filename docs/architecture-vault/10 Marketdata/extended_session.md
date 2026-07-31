# extended_session

**Anchor:** `marketdata/extended.py`

**Purpose:** Logs pre/after-hours behavior into extended_session_daily for the day's active/hot tickers (spine steps 13-14).

**Receives from:** [[MarketDataProvider]] — reads daily + intraday bars.

**Feeds:** [[extended_session]] via [[extended_session_daily]] — writes the extended-hours prints.

**Feeds:** [[api_app]] via [[extended_session_daily]] — served on ticker endpoints.

*Stage: 10 Marketdata*
