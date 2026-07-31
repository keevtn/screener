# RawItemHandler.write

**Anchor:** `ingest/raw_items_sink.py:96`

**Purpose:** THE sink. Converts a NewsItem to a row keyed by sha256(source|guid|url) and inserts ON CONFLICT DO NOTHING (the DB id is the sole dedup authority). source_class is 'structured' iff source_type in {rss,sec,fda} else 'social'.

**Receives from:** [[run_source_once]] — receives every dispatched item.

**Receives from:** [[BlueskyFirehose]] — the firehose writes matched posts through the same sink.

**Feeds:** [[run_source_once]] via [[raw_items]] — persists the content-addressed row.

**Feeds:** [[build_clusters]] via [[redis_density_counters]] — on a new row increments per-ticker intraday-density counters.

*Stage: 01 Ingest*
