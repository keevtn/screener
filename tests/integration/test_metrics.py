"""Gate 5 task 5.3: metrics — hit rate, precision/recall, coverage, lead time."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from pipeline.common.config import get_or_create_config
from pipeline.common.models import Prediction
from pipeline.grade.metrics import compute_metrics, metrics_by_config

ISSUED = datetime(2025, 3, 12, 14, 0, tzinfo=UTC)


def _pred(direction, outcome, *, resolving=None, cv="v1"):
    return Prediction(
        ticker="TICK",
        direction=direction,
        confidence=0.5,
        horizon_trading_days=3,
        threshold=0.02,
        issued_at=ISSUED,
        config_version=cv,
        evidence_json={},
        status="graded",
        outcome=outcome,
        resolving_close=resolving,
    )


def test_metrics_known_ledger():
    # 2 bull correct, 1 bull incorrect, 1 bear correct, 1 bear incorrect, 1 expired.
    preds = [
        _pred("bullish", "correct", resolving=date(2025, 3, 14)),
        _pred("bullish", "correct", resolving=date(2025, 3, 14)),
        _pred("bullish", "incorrect"),
        _pred("bearish", "correct", resolving=date(2025, 3, 14)),
        _pred("bearish", "incorrect"),
        _pred("bullish", "expired"),
    ]
    m = compute_metrics(preds, "v1")
    assert (m.correct, m.incorrect, m.expired, m.total_graded) == (3, 2, 1, 6)
    assert m.resolved == 5
    assert m.hit_rate == pytest.approx(3 / 5)
    assert m.coverage == pytest.approx(5 / 6)
    # precision(bull) = 2/(2+1); recall(bull) = 2/(2 + incorrect_bear=1).
    assert m.precision["bullish"] == pytest.approx(2 / 3)
    assert m.recall["bullish"] == pytest.approx(2 / 3)
    # precision(bear) = 1/(1+1); recall(bear) = 1/(1 + incorrect_bull=1).
    assert m.precision["bearish"] == pytest.approx(1 / 2)
    assert m.recall["bearish"] == pytest.approx(1 / 2)


def test_lead_time_known_gap():
    # Issued Wed 3/12, crossed Fri 3/14 -> 2 business days lead.
    preds = [_pred("bullish", "correct", resolving=date(2025, 3, 14))]
    m = compute_metrics(preds, "v1")
    assert m.mean_lead_time_days == pytest.approx(2.0)


def test_metrics_by_config_separates_skill_and_baseline(engine):
    with Session(engine) as s:
        real = get_or_create_config(s, notes="skill")
        base = get_or_create_config(s, {"baseline": "always_up"}, notes="baseline")
        s.add_all(
            [
                _pred("bullish", "correct", resolving=date(2025, 3, 14), cv=real.config_version),
                _pred("bullish", "incorrect", cv=real.config_version),
                _pred("bullish", "incorrect", cv=base.config_version),
            ]
        )
        s.commit()
        by_cv = metrics_by_config(s)
        assert by_cv[real.config_version].hit_rate == pytest.approx(0.5)
        assert by_cv[base.config_version].hit_rate == pytest.approx(0.0)
