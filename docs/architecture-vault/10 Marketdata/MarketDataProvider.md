# MarketDataProvider

**Anchor:** `marketdata/provider.py:101`

**Purpose:** yfinance daily bars with a 2h-TTL parquet cache; get_benchmark_bars supplies SPY, the spine's grading + calendar backbone.

**Receives from:** entry point — external feed.

**Feeds:** [[Grader.grade|grade]] — SPY + ticker adjusted closes define the grade.

**Feeds:** [[market_adjusted_reaction]] — post-event bars sign armed predictions.

**Feeds:** [[grade_premarket_panels]] — open/close bars grade the panel.

**Feeds:** [[TradingCalendar]] — SPY bars define trading days.

*Stage: 10 Marketdata*
