# social_filter.is_nsfw_post

**Anchor:** `backend/social_filter.py`

**Purpose:** NSFW/spam gate for Bluesky: rejects posts whose moderation labels or text match the blocklist before they become items.

**Feeds:** [[BlueskyExtractor]] — filters posts before parsing.

*Stage: 01 Ingest*
