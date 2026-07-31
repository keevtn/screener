# edgar_user_agent

**Anchor:** `ingest/edgar.py:21`

**Purpose:** Resolves the SEC fair-access User-Agent at call time (EDGAR_USER_AGENT -> legacy contact email -> throttled placeholder) so every EDGAR request declares a real operator.

**Feeds:** [[SECExtractor]] — supplies the compliant UA header for EDGAR polling.

*Stage: 01 Ingest*
