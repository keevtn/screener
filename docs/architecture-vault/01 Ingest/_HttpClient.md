# _HttpClient

**Anchor:** `backend/IngestionModule.py:301`

**Purpose:** Shared aiohttp wrapper for all backend extractors: certifi TLS, per-feed browser-UA override, and header access so pollers can read rate-limit headers.

**Feeds:** [[RSSExtractor]] — carries every RSS fetch.

**Feeds:** [[SECExtractor]] — carries EDGAR fetches.

**Feeds:** [[FDAExtractor]] — carries FDA fetches.

*Stage: 01 Ingest*
