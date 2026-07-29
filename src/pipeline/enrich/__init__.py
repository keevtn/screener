"""Enrichment layer (docs/ROADMAP.md Phase 2).

One story = one cluster = one scoring target attached to the right tickers:
canonical model (2.1), dedup + clustering (2.2), provenance tiers (2.3), entity
resolution with roles (2.4), and an idempotent backfill over the archive (2.5).
"""

from pipeline.enrich.canonical import CanonicalItem, from_raw_item, normalize_headline
from pipeline.enrich.cluster import (
    ClusterResult,
    build_clusters,
    persist_clusters,
)

__all__ = [
    "CanonicalItem",
    "ClusterResult",
    "build_clusters",
    "from_raw_item",
    "normalize_headline",
    "persist_clusters",
]
