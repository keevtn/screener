# BlueskyFirehose

**Anchor:** `ingest/firehose.py`

**Purpose:** A long-lived Jetstream WSS subscription in its own supervised process that keeps only posts whose text resolves a real-universe cashtag; matches land shadow-mode, deduped by post URI with the term-search lane.

**Receives from:** entry point — external feed.

**Feeds:** [[RawItemHandler.write|write]] — writes cashtag-matched posts straight to the sink.

*Stage: 01 Ingest*
