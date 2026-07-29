"""Baseline shadow predictors (docs/ROADMAP.md task 5.2).

For every real structured prediction, emit parallel baseline predictions on the
SAME ticker-day: ``always_up``, ``random`` (seeded → reproducible), and
``momentum(k)`` (sign of the trailing k-day return). Each baseline has its own
immutable config version, so the metrics module (5.3) treats skill and baselines
uniformly. Idempotent: re-running never double-writes a baseline for a ticker-day.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.config import get_or_create_config
from pipeline.common.models import Prediction
from pipeline.grade.grader import _closes_by_date

DEFAULT_SEED = 42
DEFAULT_MOMENTUM_K = 5


def ensure_baseline_configs(
    session: Session, *, seed: int = DEFAULT_SEED, momentum_k: int = DEFAULT_MOMENTUM_K
) -> dict[str, str]:
    """Create/return {baseline_name: config_version} for the three baselines."""
    return {
        "always_up": get_or_create_config(
            session, {"baseline": "always_up"}, notes="baseline always_up"
        ).config_version,
        "random": get_or_create_config(
            session, {"baseline": "random", "seed": seed}, notes="baseline random"
        ).config_version,
        "momentum": get_or_create_config(
            session, {"baseline": "momentum", "k": momentum_k}, notes="baseline momentum"
        ).config_version,
    }


def random_direction(ticker: str, issued_at: datetime, seed: int) -> str:
    """Deterministic pseudo-random direction (reproducible for a seed)."""
    key = f"{seed}|{ticker}|{issued_at.isoformat()}".encode()
    return "bullish" if int(hashlib.sha256(key).hexdigest()[:8], 16) % 2 == 0 else "bearish"


def momentum_direction(provider: Any, ticker: str, issued_at: datetime, k: int) -> str | None:
    """Sign of the trailing k-trading-day return before issue; None if no data."""
    start = issued_at.date() - timedelta(days=k * 3 + 12)
    bars = provider.get_daily_bars(ticker, start, issued_at.date())
    closes = _closes_by_date(bars)
    prior = sorted(d for d in closes if d < issued_at.date())
    if len(prior) < k + 1:
        return None
    window = prior[-(k + 1) :]
    ret = closes[window[-1]] / closes[window[0]] - 1.0
    return "bullish" if ret >= 0 else "bearish"


def _emit(
    session: Session, real: Prediction, direction: str, config_version: str, name: str
) -> bool:
    exists = session.execute(
        select(Prediction.prediction_id)
        .where(Prediction.ticker == real.ticker)
        .where(Prediction.issued_at == real.issued_at)
        .where(Prediction.config_version == config_version)
        .limit(1)
    ).first()
    if exists is not None:
        return False
    session.add(
        Prediction(
            ticker=real.ticker,
            direction=direction,
            confidence=0.5,
            horizon_trading_days=real.horizon_trading_days,
            threshold=real.threshold,
            issued_at=real.issued_at,
            config_version=config_version,
            evidence_json={"baseline": name, "shadows": real.prediction_id},
            status="open",
        )
    )
    return True


def emit_baselines(
    session: Session,
    provider: Any,
    real_config_version: str,
    *,
    seed: int = DEFAULT_SEED,
    momentum_k: int = DEFAULT_MOMENTUM_K,
) -> dict[str, str]:
    """Shadow every real prediction (of ``real_config_version``) with baselines."""
    cvs = ensure_baseline_configs(session, seed=seed, momentum_k=momentum_k)
    reals = (
        session.execute(select(Prediction).where(Prediction.config_version == real_config_version))
        .scalars()
        .all()
    )
    for real in reals:
        _emit(session, real, "bullish", cvs["always_up"], "always_up")
        _emit(
            session,
            real,
            random_direction(real.ticker, real.issued_at, seed),
            cvs["random"],
            "random",
        )
        mom = momentum_direction(provider, real.ticker, real.issued_at, momentum_k)
        if mom is not None:  # skip momentum when there is no trailing data
            _emit(session, real, mom, cvs["momentum"], "momentum")
    session.commit()
    return cvs
