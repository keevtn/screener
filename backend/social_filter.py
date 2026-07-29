"""
social_filter.py
================
Drop NSFW / adult / spam social posts before they reach the feed.

The finance keyword filter isn't enough on its own: adult-spam accounts tag posts
with finance hashtags (#crypto, #bitcoin), so the word "crypto" is in the text and
the post sails through the relevance gate. This adds a content check on the
Bluesky post itself:

  1. **Moderation labels** — Bluesky attaches adult/graphic labels (porn, sexual,
     nudity, graphic-media …) to posts and self-labels in the record. This is the
     authoritative signal and catches most of it.
  2. **Keyword backstop** — a small, conservative, word-boundary blocklist for
     clearly-adult terms that slip through unlabeled.

Pure and unit-tested; used by both the continuous social ingestion
(``UnstructuredModule``) and the on-demand cashtag search (``social_search``).
"""

from __future__ import annotations

import re
from typing import Any

# Bluesky moderation/self-label values that mark adult or graphic content.
_NSFW_LABELS: frozenset[str] = frozenset({
    "porn", "sexual", "nudity", "graphic-media", "adult",
    "sexual-figurative", "!warn", "!hide",
})

# Conservative keyword backstop for unlabeled adult/spam (word-boundary matched).
_BLOCK_TERMS: frozenset[str] = frozenset({
    "porn", "porno", "nsfw", "onlyfans", "xxx", "camgirl", "camgirls",
    "nude", "nudes", "hentai", "cumshot", "blowjob", "creampie", "milf",
})
_BLOCK_RE = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in _BLOCK_TERMS) + r")\b", re.I)


def post_labels(post: dict[str, Any]) -> set[str]:
    """All label values on a Bluesky post — moderation labels + record self-labels."""
    vals: set[str] = set()
    for lbl in post.get("labels") or []:
        if isinstance(lbl, dict) and lbl.get("val"):
            vals.add(str(lbl["val"]).lower())
    record = post.get("record") or {}
    for lbl in (record.get("labels") or {}).get("values") or []:
        if isinstance(lbl, dict) and lbl.get("val"):
            vals.add(str(lbl["val"]).lower())
    return vals


def is_blocked_text(text: str | None) -> bool:
    """True if the text matches the adult/spam keyword blocklist."""
    return bool(text) and bool(_BLOCK_RE.search(text))


def is_nsfw_post(post: dict[str, Any]) -> bool:
    """True if a Bluesky post is adult/graphic (by label) or matches the blocklist."""
    if post_labels(post) & _NSFW_LABELS:
        return True
    text = (post.get("record") or {}).get("text", "")
    return is_blocked_text(text)
