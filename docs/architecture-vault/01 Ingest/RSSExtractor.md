# RSSExtractor

**Anchor:** `backend/IngestionModule.py:686`

**Purpose:** Polls ~40 structured newswire feeds (Bloomberg, WSJ, MarketWatch, IBKR Traders' Insight, Fed/BLS, regulators, PR wires, Nasdaq halts, biotech, short-sellers, SEC-via-RSS) plus 4 Reddit multi-subreddit feeds into NewsItems.

**Receives from:** [[_HttpClient]] — uses the shared client.

**Feeds:** [[run_source_once]] — yields structured + social NewsItems for the sink.

*Stage: 01 Ingest*
