"""Task 0.2 gate test: insert config v0 + a prediction; read back intact."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from pipeline.common.models import Config, Prediction, params_hash

PARAMS_V0 = {
    "horizon_trading_days": 3,
    "threshold": 0.02,
    "benchmark_symbol": "SPY",
    "close_time": "16:00",
    "exchange_tz": "America/New_York",
    "calendar_source": "benchmark_bars",
}

ISSUED = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)


def test_db_roundtrip(engine, session):
    session.add(
        Config(
            config_version="v0",
            params_json=PARAMS_V0,
            params_hash=params_hash(PARAMS_V0),
            created_at=ISSUED,
            notes="contract v1 defaults",
        )
    )
    session.add(
        Prediction(
            prediction_id="p-roundtrip-1",
            ticker="AAPL",
            direction="bullish",
            confidence=0.7,
            horizon_trading_days=3,
            threshold=0.02,
            issued_at=ISSUED,
            config_version="v0",
            evidence_json={"cluster_ids": ["c1", "c2"]},
        )
    )
    session.commit()

    # Fresh session: nothing served from identity-map cache.
    with Session(engine) as fresh:
        cfg = fresh.get(Config, "v0")
        pred = fresh.get(Prediction, "p-roundtrip-1")

    assert cfg is not None and pred is not None
    assert cfg.params_json == PARAMS_V0
    assert cfg.params_hash == params_hash(PARAMS_V0)
    assert pred.ticker == "AAPL"
    assert pred.direction == "bullish"
    assert pred.confidence == 0.7
    assert pred.evidence_json == {"cluster_ids": ["c1", "c2"]}
    assert pred.config_version == "v0"
    assert pred.status == "open"
    assert pred.outcome is None and pred.graded_at is None

    # I1: timestamps come back tz-aware UTC, value-identical.
    assert pred.issued_at == ISSUED
    assert pred.issued_at.tzinfo is not None
    assert cfg.created_at == ISSUED
    assert cfg.created_at.tzinfo is not None
