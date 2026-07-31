# grade_premarket_panels

**Anchor:** `panel/premarket.py:337`

**Purpose:** After 16:30 ET, grades each frozen panel: open-to-close and gap returns, lean hit-rate, and a top5-vs-rest move summary, writing grade fields only.

**Receives from:** [[premarket_panel]] via [[premarket_panels]] — grades the frozen snapshot on realized moves.

**Receives from:** [[MarketDataProvider]] — reads the day's open/close bars.

**Feeds:** [[grade_premarket_panels]] via [[premarket_panels]] — stamps the panel's realized scorecard.

*Stage: 08 Panel*
