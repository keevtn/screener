# api_app

**Anchor:** `api/app.py`

**Purpose:** The read-only API: 67 endpoints (12 /trader, 12 /tickers, 6 /agents, 5 /sim, 5 /config, 5 universe/screener/fundamentals, 3 /catalysts, 3 /lab, 3 news, 2 /predictions, 2 /clusters, 8 singletons). No route can mutate an order.

**Receives from:** [[metrics_by_config]] via [[predictions]] — serves the config scoreboard.

**Receives from:** [[pair_round_trips]] via [[sim_trades]] — serves the blotter.

**Receives from:** [[PaperAccountReader]] — serves /trader state.

**Receives from:** [[health]] — aggregates health checks.

*Stage: 11 API*
