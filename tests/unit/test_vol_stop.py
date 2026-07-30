"""vol_stop exit plumbing: atr_frac is snapshotted at entry (only for vol_stop
configs) and drives the live exit, horizon-backstopped. Closes the known gap
where atr_frac was never wired and decide_exit always fell back to horizon_hold.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pipeline.marketdata.vol import atr_fraction
from pipeline.sim.engine import _open_trade, evaluate_exits
from pipeline.common.models import SimConfig, SimTrade

NOW = datetime(2026, 7, 30, 15, 0, tzinfo=UTC)


def _cfg(exit_policy=None, horizon=3):
    params = {"direction": "long", "horizon_trading_days": horizon}
    if exit_policy:
        params["exit_policy"] = exit_policy
    return SimConfig(config_id="cfg1", name="c1", created_at=NOW, params_json=params, enabled=True)


def _feat(ticker="AAPL"):
    return {"ticker": ticker, "cluster_id": "cl1", "catalyst_type": None}


def test_atr_fraction_constant_range():
    bars = [{"high": 101, "low": 99, "close": 100} for _ in range(20)]
    assert atr_fraction(bars) == 0.02  # TR=2 on close 100


def test_open_trade_snapshots_atr_only_for_vol_stop():
    vol_cfg = _cfg(exit_policy={"kind": "vol_stop", "atr_mult": 2.0})
    t = _open_trade(vol_cfg, _feat(), 1, 100.0, NOW, None, atr_fn=lambda _t: 0.03)
    assert t.features_json["atr_frac"] == 0.03

    hz_cfg = _cfg(exit_policy=None)  # horizon_hold -> no atr fetch, snapshot stays None
    called = {"n": 0}

    def atr_fn(_t):
        called["n"] += 1
        return 0.03

    t2 = _open_trade(hz_cfg, _feat(), 1, 100.0, NOW, None, atr_fn=atr_fn)
    assert t2.features_json["atr_frac"] is None
    assert called["n"] == 0  # never fetched for a horizon config


def _open_trade_row(session, exit_policy, atr_frac, entry=100.0):
    session.add(_cfg(exit_policy=exit_policy))
    session.flush()
    t = SimTrade(
        config_id="cfg1", ticker="AAPL", direction=1, entered_at=NOW, entry_price=entry,
        entry_source="alpaca-paper", horizon_trading_days=3,
        features_json={
            "config_params": {"exit_policy": exit_policy, "horizon_trading_days": 3},
            "atr_frac": atr_frac, "catalyst_type": None, "qty": 10,
        },
        status="open", created_at=NOW,
    )
    session.add(t)
    session.commit()
    return t


def test_vol_stop_exits_on_adverse_breach(session):
    # atr_frac=0.02, mult 2 -> stop at 4% adverse. Quote 95 = -5% -> breach -> exit.
    _open_trade_row(session, {"kind": "vol_stop", "atr_mult": 2.0}, 0.02)
    closed = evaluate_exits(session, quote=lambda _t: 95.0, now=NOW)
    assert len(closed) == 1
    assert closed[0].exit_reason == "vol_stop"


def test_vol_stop_holds_within_band(session):
    # Quote 97 = -3% adverse < 4% stop, horizon not reached -> hold (no exit).
    _open_trade_row(session, {"kind": "vol_stop", "atr_mult": 2.0}, 0.02)
    closed = evaluate_exits(session, quote=lambda _t: 97.0, now=NOW)
    assert closed == []


def test_vol_stop_without_atr_falls_back_to_horizon(session):
    # No frozen atr (older trade / no vol) -> vol_stop can't fire -> horizon backstop
    # holds (horizon not reached), so no early exit even on a big adverse move.
    _open_trade_row(session, {"kind": "vol_stop", "atr_mult": 2.0}, None)
    closed = evaluate_exits(session, quote=lambda _t: 80.0, now=NOW)
    assert closed == []


def test_horizon_config_ignores_adverse_move(session):
    # horizon_hold: no early exit regardless of drawdown until the horizon elapses.
    _open_trade_row(session, None, None)
    closed = evaluate_exits(session, quote=lambda _t: 80.0, now=NOW)
    assert closed == []


def test_force_flatten_closes_vol_stop_trade(session):
    # The EOD flatten backstop closes everything, never gated by any policy.
    _open_trade_row(session, {"kind": "vol_stop", "atr_mult": 2.0}, 0.02)
    closed = evaluate_exits(session, quote=lambda _t: 100.0, now=NOW, force=True)
    assert len(closed) == 1
    assert closed[0].exit_reason == "close"
