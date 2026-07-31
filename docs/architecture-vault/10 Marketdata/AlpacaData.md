# AlpacaData

**Anchor:** `marketdata/alpaca.py:48`

**Purpose:** Alpaca market data: minute + daily IEX bars, latest quotes, and the exchange clock.

**Receives from:** entry point — external feed.

**Feeds:** [[evaluate_entries]] — quotes fill paper entries.

**Feeds:** [[EOD_flatten]] — the clock times the EOD flatten.

**Feeds:** [[run_trader_driver]] — ET from the exchange clock.

*Stage: 10 Marketdata*
