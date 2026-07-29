"""Score routing metadata (docs/ROADMAP.md task 3.3).

Marks each cluster by the kind of text its origin is, so aggregation can weight
L-M higher on filings and FinBERT higher on prose (weights live in config, not
here). Text kind is derived from the source tier: tier 0 primary docs = filing,
tier 1 origin wires = press_release, tier 2/3 = article.
"""

from __future__ import annotations

from pipeline.enrich.tiers import SourceTiers

_KIND_BY_TIER = {0: "filing", 1: "press_release"}


def text_kind_of(source: str, tiers: SourceTiers) -> str:
    return _KIND_BY_TIER.get(tiers.tier_of(source), "article")
