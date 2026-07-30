"""The vol_stop A/B assignment: idempotent, walk-forward safe, subset-only."""

from __future__ import annotations

from datetime import UTC, datetime

from scripts.assign_exit_policies import VOL_STOP_CONFIGS, assign
from pipeline.common.models import SimConfig, SimTrade

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _seed_configs(session):
    # the vol_stop-arm names + one that must STAY horizon_hold
    names = sorted(VOL_STOP_CONFIGS) + ["exp-any-material"]
    for i, name in enumerate(names):
        session.add(SimConfig(
            config_id=f"c{i}", name=name, created_at=NOW,
            params_json={"direction": "long", "horizon_trading_days": 1}, enabled=True,
        ))
    session.commit()


def test_assigns_vol_stop_to_subset_only(session):
    _seed_configs(session)
    actions = assign(session)
    for name in VOL_STOP_CONFIGS:
        assert actions[name] == "assigned"
        cfg = session.execute(
            SimConfig.__table__.select().where(SimConfig.name == name)
        ).first()
        assert cfg.params_json["exit_policy"] == {"kind": "vol_stop", "atr_mult": 2.0}
    # the control config was not in the vol_stop set, so it's untouched (no key)
    control = session.execute(
        SimConfig.__table__.select().where(SimConfig.name == "exp-any-material")
    ).first()
    assert "exit_policy" not in (control.params_json or {})


def test_idempotent_second_run_is_noop(session):
    _seed_configs(session)
    assign(session)
    actions2 = assign(session)
    assert all(a == "skip-has-policy" for a in actions2.values())


def test_skips_config_with_trades(session):
    _seed_configs(session)
    # give one target config a trade BEFORE assignment -> must not retro-tune it
    target = next(iter(sorted(VOL_STOP_CONFIGS)))
    cid = session.execute(
        SimConfig.__table__.select().where(SimConfig.name == target)
    ).first().config_id
    session.add(SimTrade(
        config_id=cid, ticker="AAPL", direction=1, entered_at=NOW, entry_price=10.0,
        entry_source="alpaca-paper", horizon_trading_days=1, features_json={},
        status="open", created_at=NOW,
    ))
    session.commit()
    actions = assign(session)
    assert actions[target] == "skip-has-trades"
    cfg = session.execute(
        SimConfig.__table__.select().where(SimConfig.name == target)
    ).first()
    assert "exit_policy" not in cfg.params_json


def test_absent_config_reported(session):
    # no configs seeded at all
    actions = assign(session)
    assert all(a == "absent" for a in actions.values())
