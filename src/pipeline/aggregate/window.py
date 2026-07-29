"""Rolling window with time decay (docs/ROADMAP.md task 4.1).

Per ticker, each *cluster* (I5 — never per article copy) contributes
``tier_weight × exp(−age_hours / half_life) × value``. The sentiment and
materiality composites are the weighted SUMS of those contributions — additive, so
they decay toward zero as news ages (a stale single signal falls below threshold
after ~one half-life with no new news). Materiality is a SEPARATE windowed term,
never folded into sentiment.

ROADMAP-NOTE: weighted sum (not mean) so the signal magnitude decays with age
per the roadmap's "each cluster contributes …" formula; item count and total
weight are exposed separately for the min-items gate and confidence. The
accumulator adds order-independently, so an incremental update equals a full
recompute.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ClusterContribution:
    cluster_id: str
    sentiment: float
    materiality: float
    weight: float


@dataclass(frozen=True)
class WindowState:
    ticker: str
    sentiment_composite: float
    materiality_composite: float
    item_count: int
    total_weight: float
    contributing_cluster_ids: list[str] = field(default_factory=list)


def decay(age_hours: float, half_life_hours: float) -> float:
    """Exponential time decay; 0 if the half-life is non-positive."""
    if half_life_hours <= 0:
        return 0.0
    return math.exp(-max(0.0, age_hours) / half_life_hours)


def cluster_weight(
    tier: int | None, age_hours: float, source_class: str, params: dict[str, Any]
) -> float:
    """tier_weight × decay(age; half_life[source_class])."""
    tier_key = str(tier if tier is not None else 2)
    tier_weights = params["tier_weights"]
    tw = tier_weights.get(tier_key, tier_weights.get("2", 0.7))
    half_lives = params["half_life_hours"]
    hl = half_lives.get(source_class, half_lives.get("structured", 48.0))
    return float(tw) * decay(age_hours, float(hl))


def blended_sentiment(
    finbert_score: float | None,
    lm_score: float | None,
    params: dict[str, Any],
    text_kind: str = "article",
) -> float:
    """Blend the separately-stored model scores at aggregation time (I7).

    Blend weights come from config; the text kind refines them (L-M higher on
    filings, FinBERT higher on prose — task 3.3). Missing a model falls back to
    the one present; both missing -> 0.
    """
    weights = params.get("text_kind_blend", {}).get(text_kind) or params["blend_weights"]
    if finbert_score is None and lm_score is None:
        return 0.0
    if finbert_score is None:
        return float(lm_score)
    if lm_score is None:
        return float(finbert_score)
    return weights["finbert"] * float(finbert_score) + weights["lm"] * float(lm_score)


class WindowAccumulator:
    """Order-independent running sums, so incremental == full recompute (4.1)."""

    def __init__(self) -> None:
        self._sum_w = 0.0
        self._sum_ws = 0.0  # Σ weight·sentiment
        self._sum_wm = 0.0  # Σ weight·materiality
        self._ids: list[str] = []

    def add(self, c: ClusterContribution) -> None:
        self._sum_w += c.weight
        self._sum_ws += c.weight * c.sentiment
        self._sum_wm += c.weight * c.materiality
        self._ids.append(c.cluster_id)

    def state(self, ticker: str) -> WindowState:
        # Weighted SUMS: additive contributions that decay with age.
        return WindowState(
            ticker=ticker,
            sentiment_composite=self._sum_ws,
            materiality_composite=self._sum_wm,
            item_count=len(self._ids),
            total_weight=self._sum_w,
            contributing_cluster_ids=list(self._ids),
        )


def compute_window(ticker: str, contributions: list[ClusterContribution]) -> WindowState:
    acc = WindowAccumulator()
    for c in contributions:
        acc.add(c)
    return acc.state(ticker)
