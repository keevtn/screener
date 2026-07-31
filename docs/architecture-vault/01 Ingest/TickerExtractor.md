# TickerExtractor

**Anchor:** `backend/ticker_extractor.py:500`

**Purpose:** Extracts tickers from item text: $SYM/(SYM)/EXCHANGE:SYM patterns, then company-name + subsidiary map, then CIK, minus a false-positive list; social items are validated against the listed universe.

**Receives from:** [[RSSExtractor]] — runs on each NewsItem's text at ingest.

**Feeds:** [[RawItemHandler.write|write]] — sets NewsItem.tickers stored in payload_json.

*Stage: 01 Ingest*
