# snapshot_search_interest

**Anchor:** `ingest/trends.py:211`

**Purpose:** Google-Trends lane (own cookie-priming client): upserts each hot-set ticker's own-normalized 0-100 search series. Descriptive SHADOW axis only, run by a separate job, never a signal input.

**Receives from:** entry point — external feed.

**Feeds:** [[snapshot_search_interest]] via [[search_interest_daily]] — persists the per-ticker interest series (self-normalized anomaly, mirror of buzz_z).

*Stage: 01 Ingest*
