# health

**Anchor:** `api/app.py (/health)`

**Purpose:** The /health composite: scoring (FinBERT self-test + recent-score count), mount (st_dev volume proof), and driver (heartbeat freshness).

**Receives from:** [[score_clusters]] via [[score_status]] — reads the scoring breadcrumb.

**Receives from:** [[resolve_finbert]] via [[finbert_status]] — reads FinBERT backend health.

**Receives from:** [[Heartbeat]] via [[trader_heartbeat]] — reads driver liveness.

**Receives from:** [[volume_guard]] — reads the mount proof.

**Feeds:** [[api_app]] — surfaced on /health.

*Stage: 11 API*
