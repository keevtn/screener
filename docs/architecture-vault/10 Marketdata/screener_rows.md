# screener_rows

**Anchor:** `aggregate/screener.py:287`

**Purpose:** Builds screener rows + ticker stats (density/heat display blend) for /screener/rows and /screener/stats.

**Receives from:** [[build_attention_daily]] via [[attention_daily]] — reads attention for the screener.

**Receives from:** [[RawItemHandler.write|write]] via [[redis_density_counters]] — reads intraday density for the heat blend.

**Receives from:** [[FinvizProvider]] — reads fundamentals.

**Feeds:** [[api_app]] — served on /screener/*.

*Stage: 10 Marketdata*
