"""Phase-2 sim rails: entries, cooldown, exits, immutability, API, no-fabrication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.api import create_app
from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    FundamentalsSnapshot,
    ImmutableRowViolation,
    RawItem,
    SimConfig,
    SimTrade,
)
from pipeline.sim.engine import (
    entry_guard_status,
    evaluate_entries,
    evaluate_exits,
    run_sim_cycle,
)

NOW = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)


def _seed_cluster(s, cid, ticker, *, finbert=0.6, materiality=0.8, high_alert=True, published=None):
    published = published or (NOW - timedelta(minutes=5))
    s.add(
        RawItem(
            id=cid,
            source="Reuters",
            source_class="structured",
            url=f"https://x/{cid}",
            published_at=published,
            ingested_at=published,
            payload_json={"title": f"{ticker} news", "guid": cid},
        )
    )
    s.flush()
    s.add(
        Cluster(
            cluster_id=cid,
            origin_item_id=cid,
            member_ids_json=[cid],
            origin_tier=1,
            member_count=1,
            created_at=published,
        )
    )
    s.add(
        ClusterScore(
            cluster_id=cid,
            finbert_score=finbert,
            lm_score=finbert,
            text_kind="article",
            catalyst_type="ma",
            event_stage="announced",
            materiality=materiality,
            high_alert=high_alert,
            predictive=True,
            reaction_dependent=False,
            created_at=published,
        )
    )
    s.add(
        ClusterEntity(
            cluster_id=cid,
            ticker=ticker,
            ticker_role="subject",
            match_method="name",
            created_at=published,
        )
    )
    s.flush()


def _config(s, *, enabled=True, params=None):
    cfg = SimConfig(
        name=f"test-cfg-{enabled}-{len(params or {})}",
        created_at=NOW,
        params_json=params
        or {
            "high_alert_only": True,
            "direction": "finbert_sign",
            "direction_min_abs": 0.3,
            "horizon_trading_days": 3,
        },
        enabled=enabled,
        gate_ref="test",
    )
    s.add(cfg)
    s.commit()
    return cfg


def test_no_enabled_configs_no_trades(engine):
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL")
        _config(s, enabled=False)
        assert evaluate_entries(s, lambda t: 100.0, now=NOW) == []


def test_entry_snapshot_direction_and_no_dup(engine):
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL", finbert=0.7)
        _config(s)
        opened = evaluate_entries(s, lambda t: 100.0, now=NOW)
        assert len(opened) == 1
        t = opened[0]
        assert t.direction == 1 and t.entry_price == 100.0 and t.status == "open"
        assert t.features_json["high_alert"] is True  # decision-time snapshot
        assert t.cluster_id == "c1"
        # same sweep again: open position blocks re-entry
        assert evaluate_entries(s, lambda t: 101.0, now=NOW) == []


def test_neutral_sentiment_never_coinflipped(engine):
    with Session(engine) as s:
        _seed_cluster(s, "c1", "SDOT", finbert=0.04)  # the SDOT lesson
        _config(s)
        assert evaluate_entries(s, lambda t: 10.0, now=NOW) == []


def test_no_quote_no_trade(engine):
    with Session(engine) as s:
        _seed_cluster(s, "c1", "THIN")
        _config(s)
        assert evaluate_entries(s, lambda t: None, now=NOW) == []  # fills never fabricated


def test_max_mcap_filter(engine):
    """max_mcap_musd admits small caps, rejects big caps, and FAILS CLOSED on
    tickers with no fundamentals snapshot (unknown cap != small cap)."""
    with Session(engine) as s:
        _seed_cluster(s, "c1", "BIGCO")
        _seed_cluster(s, "c2", "TINY")
        _seed_cluster(s, "c3", "NOCAP")  # no fundamentals row at all
        s.add(
            FundamentalsSnapshot(
                ticker="BIGCO",
                as_of=NOW.date(),
                provider="test",
                market_cap=50_000.0,
                created_at=NOW,
            )
        )  # $50B in $M units
        s.add(
            FundamentalsSnapshot(
                ticker="TINY", as_of=NOW.date(), provider="test", market_cap=500.0, created_at=NOW
            )
        )  # $500M
        s.commit()
        _config(
            s,
            params={
                "high_alert_only": True,
                "direction": "finbert_sign",
                "direction_min_abs": 0.3,
                "horizon_trading_days": 0,
                "max_mcap_musd": 2000.0,
            },
        )
        opened = evaluate_entries(s, lambda t: 10.0, now=NOW)
        assert [t.ticker for t in opened] == ["TINY"]
        # decision-time cap is snapshotted with the other features
        assert opened[0].features_json["market_cap_musd"] == 500.0


def test_exit_at_horizon_with_costs(engine):
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL")
        _config(s)
        evaluate_entries(s, lambda t: 100.0, now=NOW)
        # +2 weekdays: not yet at the 3-trading-day horizon
        assert evaluate_exits(s, lambda t: 110.0, now=NOW + timedelta(days=2)) == []
        # +5 calendar days spans >=3 weekdays -> exit
        closed = evaluate_exits(s, lambda t: 110.0, now=NOW + timedelta(days=5))
        assert len(closed) == 1
        t = closed[0]
        assert t.status == "closed" and t.exit_reason == "horizon"
        assert t.gross_return == pytest.approx(0.10)
        assert t.net_return == pytest.approx(0.10 - 0.0050)


def test_closed_trades_immutable(engine):
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL")
        _config(s)
        evaluate_entries(s, lambda t: 100.0, now=NOW)
        evaluate_exits(s, lambda t: 105.0, now=NOW + timedelta(days=5))
        t = s.execute(select(SimTrade)).scalars().one()
        t.entry_price = 1.0  # tamper
        with pytest.raises(ImmutableRowViolation):
            s.commit()
        s.rollback()
        t2 = s.execute(select(SimTrade)).scalars().one()
        t2.exit_price = 999.0  # even exit fields are frozen once closed
        with pytest.raises(ImmutableRowViolation):
            s.commit()
        s.rollback()


def test_sim_api_status_toggle_trades(engine):
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL")
        cfg = _config(s)
        run_sim_cycle(s, lambda t: 100.0, now=NOW)
        cfg_id = cfg.config_id
    tc = TestClient(create_app(engine))
    status = tc.get("/sim/status").json()
    assert status["configs"] == 1 and status["open_trades"] == 1
    assert tc.get("/sim/trades").json()["items"][0]["ticker"] == "AAPL"
    toggled = tc.post(f"/sim/configs/{cfg_id}/toggle").json()
    assert toggled["enabled"] is False
    assert tc.post("/sim/configs/nope/toggle").status_code == 404


def test_sim_status_liveness_reflects_driver_activity(engine):
    """The status must show the driver trading even when SIM_ENABLED (the env flag
    the API sees, gating only the pipeline-loop path) is off — via ledger evidence,
    not the flag. Covers the 'master_enabled:false while trading' report mismatch."""
    from pipeline.common.timeutil import utcnow

    tc = TestClient(create_app(engine))

    # No trades yet -> no activity evidence, honest nulls.
    empty = tc.get("/sim/status").json()
    assert empty["recently_active"] is False
    assert empty["last_activity_at"] is None and empty["last_entry_at"] is None

    # A trade entered "now" by the standalone driver: recently_active regardless of
    # SIM_ENABLED (which is unset in the test env -> master_enabled False).
    fresh = utcnow() - timedelta(hours=1)
    with Session(engine) as s:
        cfg = _config(s)
        s.add(
            SimTrade(
                config_id=cfg.config_id,
                ticker="AAPL",
                direction=1,
                entered_at=fresh,
                entry_price=100.0,
                entry_source="alpaca-paper",
                horizon_trading_days=0,
                status="open",
                created_at=fresh,
                broker="alpaca-paper",
            )
        )
        s.commit()
    live = tc.get("/sim/status").json()
    assert live["master_enabled"] is False  # env unset -> flag off...
    assert live["recently_active"] is True  # ...but evidence shows the driver IS trading
    assert live["last_entry_at"] is not None and live["last_activity_at"] is not None
    assert live["open_trades"] == 1

    # An old-only trade (>24h) reads not-recently-active but still reports the stamp.
    with Session(engine) as s2:
        old = utcnow() - timedelta(hours=48)
        cfg2 = _config(s2, params={"direction": "long", "horizon_trading_days": 1})
        s2.add(
            SimTrade(
                config_id=cfg2.config_id,
                ticker="MSFT",
                direction=1,
                entered_at=old,
                entry_price=50.0,
                entry_source="alpaca-paper",
                horizon_trading_days=1,
                status="closed",
                exited_at=old,
                exit_price=51.0,
                exit_reason="horizon",
                gross_return=0.02,
                net_return=0.015,
                created_at=old,
                broker="alpaca-paper",
            )
        )
        s2.commit()
    # newest activity is still the 1h-old open trade -> recently_active stays True
    assert tc.get("/sim/status").json()["recently_active"] is True


# --- Entry loss guards (2026-07-28) ----------------------------------------


def _named_config(s, name, params=None):
    cfg = SimConfig(
        name=name,
        created_at=NOW,
        params_json=params
        or {
            "high_alert_only": True,
            "direction": "finbert_sign",
            "direction_min_abs": 0.3,
            "horizon_trading_days": 0,
        },
        enabled=True,
        gate_ref="test",
    )
    s.add(cfg)
    s.commit()
    return cfg


def _closed(s, cfg_id, ticker, net, *, notional=1000.0):
    """A trade that already closed earlier THIS session (exit inside the ET day)."""
    exited = NOW - timedelta(hours=1)
    s.add(
        SimTrade(
            config_id=cfg_id,
            ticker=ticker,
            direction=1,
            entered_at=NOW - timedelta(hours=3),
            entry_price=100.0,
            entry_source="alpaca-paper",
            horizon_trading_days=0,
            features_json={"notional": notional},
            status="closed",
            exited_at=exited,
            exit_price=100.0 * (1 + net),
            exit_reason="close",
            gross_return=net,
            net_return=net,
            created_at=NOW - timedelta(hours=3),
            broker="alpaca-paper",
        )
    )
    s.commit()


def test_per_config_loss_cap_halts_entries(engine, monkeypatch):
    """A config over its daily loss cap stops OPENING; a healthy sibling still does."""
    monkeypatch.setenv("SIM_CONFIG_LOSS_CAP_USD", "150")
    monkeypatch.setenv("SIM_PORTFOLIO_LOSS_CAP_USD", "100000")  # keep the book switch clear
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL", finbert=0.7)
        bleeder = _named_config(s, "bleeder")
        healthy = _named_config(s, "healthy")
        _closed(s, bleeder.config_id, "OLD", -0.20)  # -$200 <= -$150 -> capped
        opened = evaluate_entries(s, lambda t: 100.0, now=NOW)
        assert len(opened) == 1  # only the healthy config opened AAPL
        assert opened[0].config_id == healthy.config_id


def test_portfolio_kill_switch_blocks_all_entries(engine, monkeypatch):
    """Total realized day loss under the portfolio cap -> zero new entries, any config."""
    monkeypatch.setenv("SIM_CONFIG_LOSS_CAP_USD", "100000")  # keep per-config clear
    monkeypatch.setenv("SIM_PORTFOLIO_LOSS_CAP_USD", "400")
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL", finbert=0.7)
        a = _named_config(s, "a")
        b = _named_config(s, "b")
        _closed(s, a.config_id, "X", -0.25)  # -$250
        _closed(s, b.config_id, "Y", -0.25)  # -$250 -> total -$500 <= -$400
        assert evaluate_entries(s, lambda t: 100.0, now=NOW) == []


def test_loss_cap_disabled_when_zero(engine, monkeypatch):
    """A cap of 0 is the documented off-switch: entries flow despite the loss."""
    monkeypatch.setenv("SIM_CONFIG_LOSS_CAP_USD", "0")
    monkeypatch.setenv("SIM_PORTFOLIO_LOSS_CAP_USD", "0")
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL", finbert=0.7)
        cfg = _named_config(s, "bleeder")
        _closed(s, cfg.config_id, "OLD", -0.50)  # -$500, but guard disabled
        assert len(evaluate_entries(s, lambda t: 100.0, now=NOW)) == 1


def test_guards_never_block_exits(engine, monkeypatch):
    """The safety-critical property: a tripped guard blocks ENTRIES but never a
    force-flatten of open positions (a stop that can't close is worse than none)."""
    monkeypatch.setenv("SIM_PORTFOLIO_LOSS_CAP_USD", "1")  # trivially tripped
    with Session(engine) as s:
        cfg = _named_config(
            s, "c", params={"high_alert_only": True, "direction": "long", "horizon_trading_days": 0}
        )
        _closed(s, cfg.config_id, "LOSS", -0.50)  # -$500 -> book halted
        _seed_cluster(s, "c1", "AAPL", finbert=0.7)
        assert evaluate_entries(s, lambda t: 100.0, now=NOW) == []  # entries blocked
        s.add(
            SimTrade(
                config_id=cfg.config_id,
                ticker="OPENPOS",
                direction=1,
                entered_at=NOW - timedelta(hours=1),
                entry_price=100.0,
                entry_source="test",
                horizon_trading_days=0,
                features_json={"notional": 1000.0},
                status="open",
                created_at=NOW - timedelta(hours=1),
            )
        )
        s.commit()
        closed = evaluate_exits(s, lambda t: 110.0, now=NOW, force=True)
        assert len(closed) == 1 and closed[0].ticker == "OPENPOS"  # flatten NOT gated


def test_entry_guard_status_reports_state(engine, monkeypatch):
    monkeypatch.setenv("SIM_CONFIG_LOSS_CAP_USD", "150")
    monkeypatch.setenv("SIM_PORTFOLIO_LOSS_CAP_USD", "400")
    with Session(engine) as s:
        a = _named_config(s, "a")
        _named_config(s, "b")
        _closed(s, a.config_id, "X", -0.20)  # -$200 -> a halted, book not (>-$400)
        st = entry_guard_status(s, now=NOW)
        assert st["config_loss_cap_usd"] == 150.0
        assert st["portfolio_loss_cap_usd"] == 400.0
        assert st["day_realized_usd"] == -200.0
        assert st["halted_configs"] == ["a"]
        assert st["portfolio_halted"] is False


# --- Exit-policy wiring (2026-07-28) — default no-change + a policy-active exit ---


def test_default_no_exit_policy_holds_to_horizon(engine):
    """A config with no exit_policy is horizon-hold = the exact pre-policy behavior:
    an adverse intraday move does NOT exit early; the 3-day horizon still governs."""
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL", finbert=0.7)
        _config(s)  # default params, no exit_policy, horizon_trading_days=3
        evaluate_entries(s, lambda t: 100.0, now=NOW)
        # −6% an hour in: default holds (no early exit)
        assert evaluate_exits(s, lambda t: 94.0, now=NOW + timedelta(hours=1)) == []
        # still exits at the horizon, reason unchanged
        closed = evaluate_exits(s, lambda t: 94.0, now=NOW + timedelta(days=5))
        assert len(closed) == 1 and closed[0].exit_reason == "horizon"


def test_exit_policy_stop_exits_before_horizon(engine):
    """A config carrying a stop exit_policy exits early when the quote crosses it —
    well before the horizon — with an honest reason."""
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL", finbert=0.7)
        _config(
            s,
            params={
                "high_alert_only": True, "direction": "finbert_sign",
                "direction_min_abs": 0.3, "horizon_trading_days": 3,
                "exit_policy": {"kind": "stop", "stop": 0.05},
            },
        )
        opened = evaluate_entries(s, lambda t: 100.0, now=NOW)
        assert len(opened) == 1  # long @ 100; policy frozen into features_json
        closed = evaluate_exits(s, lambda t: 94.0, now=NOW + timedelta(hours=1))  # −6% -> stop
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop" and closed[0].exit_price == 94.0


def test_force_flatten_ignores_exit_policy(engine):
    """A force flatten (EOD) closes a policy trade regardless of the policy —
    a stop that never triggered must not keep a position past the close."""
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL", finbert=0.7)
        _config(
            s,
            params={
                "high_alert_only": True, "direction": "finbert_sign",
                "direction_min_abs": 0.3, "horizon_trading_days": 3,
                "exit_policy": {"kind": "stop", "stop": 0.50},  # never triggers on small moves
            },
        )
        evaluate_entries(s, lambda t: 100.0, now=NOW)
        closed = evaluate_exits(s, lambda t: 101.0, now=NOW, force=True)
        assert len(closed) == 1 and closed[0].exit_reason == "close"


def test_sim_configs_surfaces_exit_policy(engine):
    """/sim/configs exposes each config's exit policy (kind + content-addressed
    ref), so an exit-only racing variant is visibly distinct from the baseline."""
    with Session(engine) as s:
        _config(s, params={"high_alert_only": True, "direction": "long",
                           "horizon_trading_days": 0})  # no exit_policy -> horizon_hold
        _named_config = SimConfig(
            name="variant-stop", created_at=NOW, gate_ref="test", enabled=True,
            params_json={"high_alert_only": True, "direction": "long",
                         "horizon_trading_days": 0, "exit_policy": {"kind": "stop", "stop": 0.05}},
        )
        s.add(_named_config)
        s.commit()
    tc = TestClient(create_app(engine))
    items = {i["name"]: i for i in tc.get("/sim/configs").json()["items"]}
    base = items[[k for k in items if k != "variant-stop"][0]]["exit_policy"]
    var = items["variant-stop"]["exit_policy"]
    assert base["kind"] == "horizon_hold"
    assert var["kind"] == "stop"
    assert var["ref"] != base["ref"] and var["ref"].startswith("xp-")  # distinct identity
