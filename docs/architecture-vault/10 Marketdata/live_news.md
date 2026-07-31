# live_news

**Anchor:** `aggregate/news.py:104`

**Purpose:** Builds the live news tape read-model for /api/news.

**Receives from:** [[RawItemHandler.write|write]] via [[raw_items]] — reads recent items for the tape.

**Feeds:** [[api_app]] — served on /api/news.

*Stage: 10 Marketdata*
