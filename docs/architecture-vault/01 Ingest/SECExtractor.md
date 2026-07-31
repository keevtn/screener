# SECExtractor

**Anchor:** `backend/IngestionModule.py:902`

**Purpose:** Polls the EDGAR getcurrent Atom feed per filing type (8-K, 10-K, 10-Q, S-1, 425, S-4, SC 13D, DEFM14A, 424B4, ...), the primary hard-signal source for the catalyst taxonomy.

**Receives from:** [[_HttpClient]] — uses the shared client.

**Receives from:** [[edgar_user_agent]] — requires the fair-access UA.

**Feeds:** [[run_source_once]] — yields source_type=sec filing items.

*Stage: 01 Ingest*
