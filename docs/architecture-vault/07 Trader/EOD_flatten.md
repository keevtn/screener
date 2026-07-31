# EOD_flatten

**Anchor:** `sim/daily.py:39`

**Purpose:** End-of-day flatten: closes remaining positions 10 minutes before the next close (from the Alpaca clock) and rolls the day into the summary.

**Receives from:** [[decide_exit]] via [[sim_trades]] — flattens whatever the policies left open.

**Receives from:** [[AlpacaData]] — reads the exchange clock for the close time.

**Feeds:** [[EOD_flatten]] via [[sim_daily_summary]] — writes the daily P&L rollup.

**Feeds:** [[EOD_flatten]] via [[sim_trades]] — stamps the exit fills.

*Stage: 07 Trader*
