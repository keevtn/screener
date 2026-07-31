# TradingCalendar

**Anchor:** `marketdata/calendar.py:30`

**Purpose:** SPY-derived trading calendar (I10): one adjusted close per bar = one trading day, the clock every horizon and CAR window counts on.

**Receives from:** [[MarketDataProvider]] — built from SPY bars.

**Feeds:** [[Grader.grade|grade]] — defines the horizon days.

**Feeds:** [[premarket_panel]] — defines the session boundaries.

*Stage: 10 Marketdata*
