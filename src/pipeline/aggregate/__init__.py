"""Aggregation: rolling windows, time decay, buzz (docs/ROADMAP.md Phase 4)."""

from pipeline.aggregate.window import (
    ClusterContribution,
    WindowAccumulator,
    WindowState,
    blended_sentiment,
    cluster_weight,
    compute_window,
    decay,
)

__all__ = [
    "ClusterContribution",
    "WindowAccumulator",
    "WindowState",
    "blended_sentiment",
    "cluster_weight",
    "compute_window",
    "decay",
]
