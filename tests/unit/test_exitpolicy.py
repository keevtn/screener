"""Exit-policy resolution, content-addressing, live decision, and lab replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pipeline.sim.exitpolicy import (
    DEFAULT_POLICY,
    decide_exit,
    exit_policy_ref,
    replay_exit,
    resolve_exit_policy,
)


def _bar(high, low):
    return {"open": (high + low) / 2, "high": high, "low": low, "close": (high + low) / 2}


# --- resolve + content-address ---------------------------------------------
def test_resolve_defaults_to_horizon_hold():
    assert resolve_exit_policy({}, None) == DEFAULT_POLICY
    assert resolve_exit_policy({}, "ma")["kind"] == "horizon_hold"


def test_resolve_single_and_per_catalyst_precedence():
    single = {"kind": "stop", "stop": 0.05}
    by_ct = {"secondary_offering": {"kind": "bracket", "stop": 0.03, "take": 0.10}}
    params = {"exit_policy": single, "exit_policy_by_catalyst": by_ct}
    # per-catalyst-type wins when it matches
    assert resolve_exit_policy(params, "secondary_offering")["kind"] == "bracket"
    # falls back to the single policy otherwise
    assert resolve_exit_policy(params, "ma") == single


def test_ref_is_content_addressed_and_order_independent():
    a = exit_policy_ref({"kind": "bracket", "stop": 0.03, "take": 0.1})
    b = exit_policy_ref({"take": 0.1, "kind": "bracket", "stop": 0.03})
    assert a == b and a.startswith("xp-")
    assert exit_policy_ref(None) == exit_policy_ref(DEFAULT_POLICY)
    assert exit_policy_ref({"kind": "stop", "stop": 0.03}) != a


# --- live decision ----------------------------------------------------------
def _ctx(price, *, entry=100.0, direction=1, horizon=False, held=0.0, peak=None, atr=None):
    return {
        "entry": entry, "price": price, "direction": direction,
        "horizon_reached": horizon, "horizon_reason": "horizon",
        "held_hours": held, "peak_favorable": peak, "atr_frac": atr,
    }


def test_decide_horizon_hold_is_current_behavior():
    p = {"kind": "horizon_hold"}
    assert decide_exit(p, _ctx(90.0, horizon=False)).exit is False  # holds
    d = decide_exit(p, _ctx(90.0, horizon=True))
    assert d.exit is True and d.reason == "horizon"


def test_decide_stop_and_horizon_backstop():
    p = {"kind": "stop", "stop": 0.05}
    assert decide_exit(p, _ctx(96.0)).exit is False       # -4% not past -5%
    assert decide_exit(p, _ctx(94.0)).reason == "stop"    # -6% -> stop
    # stop not hit but horizon reached -> still exits (max-hold backstop)
    assert decide_exit(p, _ctx(101.0, horizon=True)).exit is True


def test_decide_bracket_take_and_short_direction():
    p = {"kind": "bracket", "stop": 0.05, "take": 0.10}
    assert decide_exit(p, _ctx(111.0)).reason == "take"   # +11% long -> take
    # short: price DOWN is favorable
    assert decide_exit(p, _ctx(88.0, direction=-1)).reason == "take"
    assert decide_exit(p, _ctx(106.0, direction=-1)).reason == "stop"  # short, price up -6%


def test_decide_time_decay():
    td = {"kind": "time_decay", "max_hold_hours": 2.0}
    assert decide_exit(td, _ctx(100.0, held=2.5)).reason == "time_decay"
    assert decide_exit(td, _ctx(100.0, held=1.0)).exit is False


def test_decide_trailing_and_volstop():
    tr = {"kind": "trailing_after_threshold", "arm": 0.05, "trail": 0.03}
    # peak +8%, now +4% -> 4% retrace (>= 3% trail), armed -> exit
    assert decide_exit(tr, _ctx(104.0, peak=0.08)).reason == "trailing"
    # no peak state live yet -> horizon backstop only
    assert decide_exit(tr, _ctx(104.0, peak=None)).exit is False
    vs = {"kind": "vol_stop", "atr_mult": 2.0}
    # atr 2% * mult 2 = 4% stop; price -5% triggers, -3% does not
    assert decide_exit(vs, _ctx(95.0, atr=0.02)).reason == "vol_stop"
    assert decide_exit(vs, _ctx(97.0, atr=0.02)).exit is False
    assert decide_exit(vs, _ctx(95.0, atr=None)).exit is False  # no atr -> backstop


# --- lab replay -------------------------------------------------------------
def _rp(policy, path, baseline, *, direction=1, atr=None):
    return replay_exit(
        policy, path, entry=100.0, direction=direction, baseline_exit=baseline, atr_frac=atr
    )


def test_replay_horizon_hold_keeps_baseline():
    px, reason = _rp({"kind": "horizon_hold"}, [_bar(105, 95)], 103.0)
    assert px == 103.0 and reason == "horizon"


def test_replay_stop_and_take_and_none():
    path = [_bar(101, 99), _bar(102, 94), _bar(108, 101)]  # bar2 lows -6%, bar3 highs +8%
    px, r = _rp({"kind": "stop", "stop": 0.05}, path, 107.0)
    assert r == "stop" and px == 95.0
    px, r = _rp({"kind": "bracket", "stop": 0.20, "take": 0.05}, path, 107.0)
    assert r == "take" and px == 105.0  # +5% take reached before a -20% stop
    px, r = _rp({"kind": "stop", "stop": 0.50}, path, 107.0)
    assert r == "horizon" and px == 107.0  # never triggered -> baseline


def test_replay_stop_first_on_same_bar():
    path = [_bar(112, 94)]  # one bar touches both +12% take and -6% stop
    px, r = _rp({"kind": "bracket", "stop": 0.05, "take": 0.10}, path, 100.0)
    assert r == "stop" and px == 95.0  # conservative: stop assumed first


def test_replay_vol_stop_scales_with_atr():
    # atr 1% * mult 2 = 2% stop -> a -4% bar triggers
    px, r = _rp({"kind": "vol_stop", "atr_mult": 2.0}, [_bar(101, 96)], 100.0, atr=0.01)
    assert r == "vol_stop" and px == 98.0


def test_replay_time_decay_exits_at_max_hold():
    t0 = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    bars = [
        {"high": 102, "low": 100, "close": 101, "time": (t0 + timedelta(minutes=30)).isoformat()},
        {"high": 103, "low": 101, "close": 102, "time": (t0 + timedelta(minutes=90)).isoformat()},
    ]
    px, r = replay_exit(
        {"kind": "time_decay", "max_hold_hours": 1.0}, bars,
        entry=100.0, direction=1, baseline_exit=105.0, entered_at=t0,
    )
    assert r == "time_decay" and px == 102.0  # first bar past +1h -> exit at its close
    # without entered_at the lab can't evaluate elapsed -> baseline
    px, r = replay_exit(
        {"kind": "time_decay", "max_hold_hours": 1.0}, bars,
        entry=100.0, direction=1, baseline_exit=105.0,
    )
    assert r == "horizon" and px == 105.0
