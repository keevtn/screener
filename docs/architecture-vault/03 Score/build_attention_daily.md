# build_attention_daily

**Anchor:** `aggregate/attention.py:73`

**Purpose:** Full idempotent recompute of the daily attention rollup: per (ticker, date) struct_count, social_count and mean FinBERT sentiment, summed across the live DB and legacy archive.

**Receives from:** [[_persist_and_resolve]] via [[cluster_entities]] — groups by attributed ticker.

**Receives from:** [[score_clusters]] via [[cluster_scores]] — averages finbert_score into sentiment_mean.

**Receives from:** [[RawItemHandler.write|write]] via [[raw_items]] — dates and classifies each contributing item.

**Feeds:** [[compute_buzz_baselines]] via [[attention_daily]] — the social-volume history baselines are fit on.

*Stage: 03 Score*
