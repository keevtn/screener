# BlueskyExtractor

**Anchor:** `backend/UnstructuredModule.py:238`

**Purpose:** Polls Bluesky searchPosts over ~26 finance terms, NSFW-gated, into social NewsItems archived shadow-mode (never scored).

**Receives from:** entry point — external feed.

**Feeds:** [[run_source_once]] — yields source_type=social items.

*Stage: 01 Ingest*
