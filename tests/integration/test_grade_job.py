"""Gate 5 task 5.1: nightly grading job grades open predictions, idempotently (I4)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.config import get_or_create_config
from pipeline.common.models import Prediction
from pipeline.grade import grade_open_predictions

PRICES = Path(__file__).resolve().parents[1] / "fixtures" / "prices"
ISSUED = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)  # Wed during hours -> C0 = Mar 12


def _csv(name):
    return pd.read_csv(PRICES / name, parse_dates=["date"])


def _seed_open_pred(session, cfg):
    session.add(
        Prediction(
            prediction_id="p-grade-1",
            ticker="TICK",
            direction="bullish",
            confidence=0.7,
            horizon_trading_days=3,
            threshold=0.02,
            issued_at=ISSUED,
            config_version=cfg.config_version,
            evidence_json={"cluster_ids": ["c1"]},
            status="open",
        )
    )
    session.commit()


def test_grade_job_idempotent(engine, make_provider):
    provider = make_provider({"TICK": _csv("correct_TICK.csv"), "SPY": _csv("correct_SPY.csv")})
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed_open_pred(s, cfg)

        graded, left_open = grade_open_predictions(s, provider)
        assert (graded, left_open) == (1, 0)
        pred = s.get(Prediction, "p-grade-1")
        assert pred.status == "graded"
        assert pred.outcome == "correct"
        graded_at_first = pred.graded_at

        # Re-run: the already-graded row is skipped -> nothing regraded (I4).
        graded2, left_open2 = grade_open_predictions(s, provider)
        assert (graded2, left_open2) == (0, 0)
        pred = s.get(Prediction, "p-grade-1")
        assert pred.graded_at == graded_at_first  # untouched
        assert s.execute(select(func.count()).select_from(Prediction)).scalar_one() == 1


def test_open_prediction_without_bars_stays_open(engine, make_provider):
    # A future-dated prediction whose horizon hasn't elapsed -> stays open.
    provider = make_provider({"TICK": _csv("correct_TICK.csv"), "SPY": _csv("correct_SPY.csv")})
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        session_pred = Prediction(
            prediction_id="p-future",
            ticker="TICK",
            direction="bullish",
            confidence=0.7,
            horizon_trading_days=3,
            threshold=0.02,
            issued_at=datetime(2099, 1, 1, 18, 0, tzinfo=UTC),
            config_version=cfg.config_version,
            evidence_json={},
            status="open",
        )
        s.add(session_pred)
        s.commit()
        graded, left_open = grade_open_predictions(s, provider)
        assert (graded, left_open) == (0, 1)
        assert s.get(Prediction, "p-future").status == "open"
