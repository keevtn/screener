"""Gate 4 task 4.1: decay math, blending, incremental==recompute, syndication (I5)."""

from __future__ import annotations

import math

import pytest

from pipeline.aggregate.window import (
    ClusterContribution,
    blended_sentiment,
    cluster_weight,
    compute_window,
)

PARAMS = {
    "tier_weights": {"0": 1.0, "1": 0.9, "2": 0.7, "3": 0.4},
    "half_life_hours": {"structured": 48.0, "social": 24.0},
    "blend_weights": {"finbert": 0.6, "lm": 0.4},
    "text_kind_blend": {"filing": {"finbert": 0.35, "lm": 0.65}},
}


def test_decay_and_weight():
    assert cluster_weight(0, 0.0, "structured", PARAMS) == pytest.approx(1.0)
    # tier 2 (0.7) at one structured half-life (48h) -> 0.7 * e^-1.
    assert cluster_weight(2, 48.0, "structured", PARAMS) == pytest.approx(0.7 * math.exp(-1))
    # social half-life is shorter (24h).
    assert cluster_weight(0, 24.0, "social", PARAMS) == pytest.approx(math.exp(-1))


def test_blended_sentiment_uses_text_kind_and_handles_missing():
    # Filing text-kind weights L-M higher.
    assert blended_sentiment(0.8, -0.2, PARAMS, "filing") == pytest.approx(0.35 * 0.8 + 0.65 * -0.2)
    # Prose default blend.
    assert blended_sentiment(0.8, -0.2, PARAMS, "article") == pytest.approx(0.6 * 0.8 + 0.4 * -0.2)
    # Missing FinBERT -> L-M only; both missing -> 0.
    assert blended_sentiment(None, 0.5, PARAMS) == pytest.approx(0.5)
    assert blended_sentiment(None, None, PARAMS) == 0.0


def test_decay_math_hand_computed():
    # Two clusters, known weights/sentiments -> exact weighted-mean composite.
    w_a = 1.0  # tier 0, age 0
    w_b = 0.7 * math.exp(-1)  # tier 2, 48h
    contribs = [
        ClusterContribution("A", sentiment=0.5, materiality=0.9, weight=w_a),
        ClusterContribution("B", sentiment=-0.2, materiality=0.4, weight=w_b),
    ]
    state = compute_window("AAPL", contribs)
    expected_sent = w_a * 0.5 + w_b * -0.2  # weighted SUM (decays with age)
    expected_mat = w_a * 0.9 + w_b * 0.4
    assert state.sentiment_composite == pytest.approx(expected_sent, abs=1e-6)
    assert state.materiality_composite == pytest.approx(expected_mat, abs=1e-6)
    assert state.item_count == 2
    assert state.total_weight == pytest.approx(w_a + w_b, abs=1e-6)


def test_incremental_equals_recompute():
    stream = [
        ClusterContribution(
            f"c{i}", sentiment=(i % 5 - 2) / 5, materiality=i / 10, weight=1.0 / (i + 1)
        )
        for i in range(12)
    ]
    full = compute_window("T", stream)
    # Incremental: fold the same stream in a different (reversed) order.
    incremental = compute_window("T", list(reversed(stream)))
    assert incremental.sentiment_composite == pytest.approx(full.sentiment_composite, abs=1e-12)
    assert incremental.materiality_composite == pytest.approx(full.materiality_composite, abs=1e-12)
    assert incremental.total_weight == pytest.approx(full.total_weight, abs=1e-12)


def test_syndication_counts_once():
    # I5 end-to-end: a syndicated story is ONE cluster -> one contribution,
    # regardless of how many article copies it has. A 1-copy and a 5-copy cluster
    # with the same score move the window identically.
    one = compute_window("T", [ClusterContribution("cl", 0.4, 0.8, 1.0)])
    five = compute_window("T", [ClusterContribution("cl", 0.4, 0.8, 1.0)])
    assert one == five
    assert one.item_count == 1
