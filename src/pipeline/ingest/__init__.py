"""Ingestion refit onto the SQLite spine (docs/ROADMAP.md Phase 1).

The existing aiohttp extractors in backend/ keep parsing feeds; this package is
the new sink + hardening layer they feed into: raw_items writing with
deterministic ids (1.1), ingest-latency logging (1.4), and the shadow-mode
scorer guard (1.5).
"""

from pipeline.ingest.edgar import edgar_user_agent
from pipeline.ingest.ratelimit import RateLimiter
from pipeline.ingest.raw_items_sink import (
    RawItemHandler,
    build_raw_item_values,
    source_class_of,
)
from pipeline.ingest.shadow import scorer_visible_stmt

__all__ = [
    "RateLimiter",
    "RawItemHandler",
    "build_raw_item_values",
    "edgar_user_agent",
    "scorer_visible_stmt",
    "source_class_of",
]
