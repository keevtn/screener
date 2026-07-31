# universe_loaders

**Anchor:** `backend/edgar_tickers.py + listed_symbols.py`

**Purpose:** 24h-TTL loaders of the valid-ticker universe (SEC company_tickers.json; Nasdaq listed/other symbol directories) used to validate social cashtags.

**Receives from:** entry point — external feed.

**Feeds:** [[TickerExtractor]] — installs the universe used for social-ticker validation.

*Stage: 01 Ingest*
