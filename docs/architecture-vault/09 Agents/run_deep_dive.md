# run_deep_dive

**Anchor:** `agents/deepdive.py:419`

**Purpose:** On-demand per-ticker deep dive over own-data-only (rate-limited 2/5min), writing a ticker_analyses row.

**Receives from:** [[default_client]] — uses the LLM client.

**Feeds:** [[run_deep_dive]] via [[ticker_analyses]] — writes the analysis.

**Feeds:** [[api_app]] via [[ticker_analyses]] — served on /agents tickers.

*Stage: 09 Agents*
