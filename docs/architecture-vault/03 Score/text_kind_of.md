# text_kind_of

**Anchor:** `score/routing.py:16`

**Purpose:** Maps a source tier to text kind (filing / press_release / article) so aggregation weights Loughran-McDonald higher on filings and FinBERT higher on prose.

**Receives from:** [[SourceTiers.tier_of|tier_of]] — reads the source tier.

**Feeds:** [[score_clusters]] — stamps text_kind onto the score row.

*Stage: 03 Score*
