"""Gate 4 task 4.3: versioned, content-addressed, immutable config (I3)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from pipeline.common.config import DEFAULT_PARAMS_V1, get_or_create_config
from pipeline.common.models import Config, ImmutableRowViolation, params_hash


def test_default_params_carry_contract_and_signal_knobs():
    p = DEFAULT_PARAMS_V1
    # Contract constants (prediction-contract-v1.md §7) live in config.
    assert p["horizon_trading_days"] == 3 and p["threshold"] == 0.02
    # Plus the Phase-4 aggregation/signal knobs.
    assert "half_life_hours" in p and "tier_weights" in p and "min_items" in p
    assert p["armed"]["enabled_types"] == ["earnings_results"]


def test_same_params_resolve_to_same_version(session):
    a = get_or_create_config(session)
    b = get_or_create_config(session)  # idempotent
    assert a.config_version == b.config_version
    assert a.params_hash == params_hash(DEFAULT_PARAMS_V1)
    assert session.execute(select(func.count()).select_from(Config)).scalar_one() == 1


def test_changed_params_create_new_version(session):
    v1 = get_or_create_config(session)
    v2 = get_or_create_config(session, {**DEFAULT_PARAMS_V1, "threshold": 0.03}, notes="theta 3%")
    assert v2.config_version != v1.config_version
    assert session.execute(select(func.count()).select_from(Config)).scalar_one() == 2


def test_config_row_is_immutable(session):
    cfg = get_or_create_config(session)
    cfg.params_json = {**DEFAULT_PARAMS_V1, "min_items": 5}
    with pytest.raises(ImmutableRowViolation):
        session.commit()
    session.rollback()
