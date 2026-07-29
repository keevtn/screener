"""Gate 2 task 2.3: source provenance tiers + origin tiebreak."""

from __future__ import annotations

from datetime import UTC, datetime

from pipeline.enrich.canonical import from_values as canonical
from pipeline.enrich.cluster import build_clusters
from pipeline.enrich.tiers import SourceTiers, load_source_tiers

BASE = datetime(2025, 3, 12, 10, 0, tzinfo=UTC)


def test_tier_mapping_from_config():
    tiers = load_source_tiers()
    assert tiers.tier_of("SEC EDGAR — 8-K") == 0
    assert tiers.tier_of("FDA Press Releases") == 0
    assert tiers.tier_of("Business Wire") == 1
    assert tiers.tier_of("Reuters") == 2
    assert tiers.tier_of("Yahoo Finance") == 3
    assert tiers.tier_of("Some Unknown Blog") == 2  # default_tier
    assert tiers.tier3_handling == "down_weight"


def test_lowest_tier_wins_on_multi_match():
    # If a label somehow matched several patterns, the most authoritative wins.
    tiers = SourceTiers({0: ["edgar"], 3: ["news"]}, default_tier=2)
    assert tiers.tier_of("EDGAR news wire") == 0


def test_origin_tiebreak_by_tier():
    tiers = load_source_tiers()
    headline = "Acme Corp Announces Record Quarterly Revenue"
    # Identical timestamps, different tiers -> lower tier is the origin.
    items = [
        canonical(
            id="agg",
            source="Yahoo Finance",
            source_class="structured",
            title=headline,
            published_at=BASE,
        ),
        canonical(
            id="wire",
            source="Business Wire",
            source_class="structured",
            title=headline,
            published_at=BASE,
        ),
    ]
    clusters = build_clusters(items, tier_of=tiers.tier_of)
    assert len(clusters) == 1
    assert clusters[0].origin_item_id == "wire"  # tier 1 beats tier 3
    assert clusters[0].origin_tier == 1
