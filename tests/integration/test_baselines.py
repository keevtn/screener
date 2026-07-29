"""Gate 5 task 5.2: baseline shadow predictors — deterministic, uniform, idempotent."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.config import get_or_create_config
from pipeline.common.models import Prediction
from pipeline.grade.baselines import emit_baselines, momentum_direction, random_direction

ISSUED = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)


def _uptrend_bars():
    # Rising weekday closes before the issue date -> momentum is bullish.
    dates = pd.bdate_range("2025-02-24", "2025-03-11")
    return pd.DataFrame({"date": dates, "adj_close": [90 + i for i in range(len(dates))]})


def _seed_real(session):
    cfg = get_or_create_config(session)
    session.add(
        Prediction(
            prediction_id="real-1",
            ticker="TICK",
            direction="bullish",
            confidence=0.8,
            horizon_trading_days=3,
            threshold=0.02,
            issued_at=ISSUED,
            config_version=cfg.config_version,
            evidence_json={},
            status="open",
        )
    )
    session.commit()
    return cfg


def test_random_direction_deterministic():
    a = random_direction("TICK", ISSUED, 42)
    b = random_direction("TICK", ISSUED, 42)
    assert a == b  # reproducible for a seed
    assert a in ("bullish", "bearish")
    # A different seed can differ; the function is a pure hash of inputs.
    assert random_direction("TICK", ISSUED, 42) == random_direction("TICK", ISSUED, 42)


def test_momentum_direction_from_trend(make_provider):
    provider = make_provider({"TICK": _uptrend_bars()})
    assert momentum_direction(provider, "TICK", ISSUED, 5) == "bullish"


def test_emit_baselines_uniform_and_idempotent(engine, make_provider):
    provider = make_provider({"TICK": _uptrend_bars()})
    with Session(engine) as s:
        cfg = _seed_real(s)
        cvs = emit_baselines(s, provider, cfg.config_version, seed=42)

        # One shadow per baseline on the same ticker-day.
        for name, cv in cvs.items():
            preds = (
                s.execute(select(Prediction).where(Prediction.config_version == cv)).scalars().all()
            )
            assert len(preds) == 1, name
            assert preds[0].ticker == "TICK" and preds[0].issued_at == ISSUED

        always_up = s.execute(
            select(Prediction).where(Prediction.config_version == cvs["always_up"])
        ).scalar_one()
        assert always_up.direction == "bullish"
        momentum = s.execute(
            select(Prediction).where(Prediction.config_version == cvs["momentum"])
        ).scalar_one()
        assert momentum.direction == "bullish"  # up-trend
        rnd = s.execute(
            select(Prediction).where(Prediction.config_version == cvs["random"])
        ).scalar_one()
        assert rnd.direction == random_direction("TICK", ISSUED, 42)

        # 1 real + 3 baselines.
        assert s.execute(select(func.count()).select_from(Prediction)).scalar_one() == 4

        # Re-run: no duplicate baseline rows.
        emit_baselines(s, provider, cfg.config_version, seed=42)
        assert s.execute(select(func.count()).select_from(Prediction)).scalar_one() == 4
