"""Extended-session tracker: pure summarizer, two-phase logging, scope, read APIs."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pipeline.api import create_app
from pipeline.common.models import ExtendedSessionDaily, PremarketPanel, SimConfig, SimTrade
from pipeline.marketdata.extended import (
    active_extended_tickers,
    extended_history,
    extended_movers,
    log_extended_session,
    premarket_streak,
    summarize_extended,
)

DAY = date(2026, 7, 24)
NOW = datetime(2026, 7, 24, 13, 40, tzinfo=UTC)  # ~09:40 ET


def _epoch(d: date, hh: int, mm: int) -> int:
    """ET-shifted epoch the way the intraday cache stores 'time' (ET wall as UTC)."""
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC).timestamp())


def _bar(d, hh, mm, o, h, lo, c, v):
    return {"time": _epoch(d, hh, mm), "open": o, "high": h, "low": lo, "close": c, "volume": v}


# --- pure summarizer + streak ------------------------------------------------
def test_summarize_splits_pre_regular_after():
    bars = [
        _bar(DAY, 8, 0, 10.0, 10.3, 9.9, 10.2, 1000),  # premarket (m=480)
        _bar(DAY, 9, 0, 10.2, 10.4, 10.1, 10.35, 2000),  # premarket
        _bar(DAY, 9, 30, 10.4, 10.5, 10.3, 10.45, 9000),  # regular open (m=570)
        _bar(DAY, 15, 59, 10.6, 10.7, 10.5, 10.6, 8000),  # regular close (m=959)
        _bar(DAY, 16, 30, 10.7, 10.9, 10.6, 10.8, 500),  # afterhours (m=990)
    ]
    from pipeline.marketdata.extended import _norm_yf_bars

    s = summarize_extended(_norm_yf_bars(bars), prior_close=10.0)
    assert s["pm_last"] == 10.35 and s["pm_pct"] == 0.035  # last premarket vs prior close
    assert s["pm_high"] == 10.4 and s["pm_low"] == 9.9 and s["pm_volume"] == 3000
    assert s["reg_open"] == 10.4 and s["reg_close"] == 10.6
    assert s["reg_pct"] == 0.06  # 10.6/10.0 - 1
    assert s["ah_last"] == 10.8 and s["ah_pct"] == round(10.8 / 10.6 - 1, 6)
    assert s["ah_volume"] == 500


def test_summarize_honest_nulls_when_no_extended():
    # Only regular bars -> premarket/afterhours are None, never fabricated.
    bars = [_bar(DAY, 10, 0, 5.0, 5.1, 4.9, 5.05, 100)]
    from pipeline.marketdata.extended import _norm_yf_bars

    s = summarize_extended(_norm_yf_bars(bars), prior_close=None)
    assert s["pm_last"] is None and s["pm_pct"] is None and s["ah_last"] is None
    assert s["reg_pct"] is None  # prior_close unknown -> no fabricated %


def test_premarket_streak():
    assert premarket_streak([0.01, 0.02, 0.03])["direction"] == "gain"
    assert premarket_streak([0.01, 0.02, 0.03])["count"] == 3
    assert premarket_streak([-0.01, 0.02, 0.03]) == {"direction": "gain", "count": 2}
    assert premarket_streak([0.01, None, 0.03]) == {"direction": "gain", "count": 1}
    assert premarket_streak([0.01, -0.02])["direction"] == "loss"
    assert premarket_streak([])["count"] == 0
    assert premarket_streak([0.01, None]) == {"direction": None, "count": 0}


# --- two-phase logging (non-clobber) -----------------------------------------
def _fns(intraday_bars_by_phase, daily):
    def intraday_fn(t):
        return {"available": True, "bars": intraday_bars_by_phase}

    def daily_bars_fn(t, start, end):
        return daily

    return intraday_fn, daily_bars_fn


def test_two_phase_premarket_then_postmarket(engine):
    prior = date(2026, 7, 23)
    # PREMARKET phase: only pre + early-regular bars; daily cache has prior close
    # but NOT today's bar (not formed yet) -> reg_close must stay NULL.
    pre_bars = [
        _bar(DAY, 8, 30, 10.0, 10.2, 9.9, 10.1, 500),  # premarket
        _bar(DAY, 9, 30, 10.3, 10.4, 10.2, 10.35, 4000),  # regular open only
    ]
    daily_pre = {prior: {"open": 9.5, "close": 10.0}}
    intr, dly = _fns(pre_bars, daily_pre)
    with Session(engine) as s:
        log_extended_session(
            s, ["AAA"], now=NOW, session_over=False, intraday_fn=intr, daily_bars_fn=dly
        )
        r = s.get(ExtendedSessionDaily, ("AAA", DAY))
        assert r.prior_close == 10.0 and r.pm_last == 10.1 and r.pm_pct == round(10.1 / 10.0 - 1, 6)
        assert r.reg_open == 10.3  # from the intraday regular-open bar
        assert r.reg_close is None and r.ah_last is None  # session not over

    # POSTMARKET phase: full day incl. afterhours; daily cache now has today's close.
    post_bars = pre_bars + [
        _bar(DAY, 15, 59, 10.5, 10.6, 10.4, 10.55, 7000),  # regular close
        _bar(DAY, 16, 30, 10.6, 10.8, 10.5, 10.7, 300),  # afterhours
    ]
    daily_post = {prior: {"open": 9.5, "close": 10.0}, DAY: {"open": 10.3, "close": 10.55}}
    intr2, dly2 = _fns(post_bars, daily_post)
    with Session(engine) as s:
        log_extended_session(
            s, ["AAA"], now=NOW, session_over=True, intraday_fn=intr2, daily_bars_fn=dly2
        )
        r = s.get(ExtendedSessionDaily, ("AAA", DAY))
        # premarket fields preserved (non-clobber), post fields filled
        assert r.pm_last == 10.1  # NOT wiped
        assert r.reg_close == 10.55 and r.reg_pct == round(10.55 / 10.0 - 1, 6)
        assert r.ah_last == 10.7 and r.ah_pct == round(10.7 / 10.55 - 1, 6) and r.ah_volume == 300


def test_no_extended_prints_stores_nulls(engine):
    # Thin name: intraday unavailable -> row still logged with regular data, pm/ah NULL.
    def intraday_fn(t):
        return {"available": False, "bars": []}

    def daily_bars_fn(t, start, end):
        return {date(2026, 7, 23): {"open": 1.0, "close": 1.1}, DAY: {"open": 1.1, "close": 1.2}}

    with Session(engine) as s:
        log_extended_session(
            s,
            ["THIN"],
            now=NOW,
            session_over=True,
            intraday_fn=intraday_fn,
            daily_bars_fn=daily_bars_fn,
        )
        r = s.get(ExtendedSessionDaily, ("THIN", DAY))
        assert r.prior_close == 1.1 and r.reg_close == 1.2
        assert r.pm_last is None and r.ah_last is None  # honest dashes downstream


# --- scope -------------------------------------------------------------------
def test_active_tickers_union_panel_and_sim(engine):
    with Session(engine) as s:
        s.add(
            PremarketPanel(
                session_date=DAY,
                computed_at=NOW,
                window_start=NOW,
                window_end=NOW,
                rows_json=[{"ticker": "PMA"}, {"ticker": "PMB"}],
                created_at=NOW,
            )
        )
        cfg = SimConfig(name="c", created_at=NOW, params_json={}, enabled=True, gate_ref="t")
        s.add(cfg)
        s.flush()
        s.add(
            SimTrade(
                config_id=cfg.config_id,
                ticker="SIMX",
                direction=1,
                entered_at=NOW - timedelta(hours=2),
                entry_price=5.0,
                entry_source="alpaca-paper",
                horizon_trading_days=0,
                status="open",
                created_at=NOW,
            )
        )
        s.commit()
        tickers = active_extended_tickers(s, NOW)
        assert set(tickers) == {"PMA", "PMB", "SIMX"}


# --- read side + endpoints ---------------------------------------------------
def _seed_history(engine):
    with Session(engine) as s:
        # AAA: 3 straight premarket gains; BBB: one big premarket drop today.
        for i, pct in enumerate([0.01, 0.02, 0.03]):
            d = DAY - timedelta(days=2 - i)
            s.add(
                ExtendedSessionDaily(
                    ticker="AAA",
                    session_date=d,
                    prior_close=10.0,
                    pm_last=10.0 * (1 + pct),
                    pm_pct=pct,
                    reg_close=10.0 * (1 + pct / 2),
                    reg_pct=pct / 2,
                    source="test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        s.add(
            ExtendedSessionDaily(
                ticker="BBB",
                session_date=DAY,
                prior_close=20.0,
                pm_last=19.0,
                pm_pct=-0.05,
                reg_close=18.5,
                reg_pct=-0.075,
                ah_last=18.7,
                ah_pct=round(18.7 / 18.5 - 1, 6),
                source="test",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        s.commit()


def test_movers_and_history(engine):
    _seed_history(engine)
    with Session(engine) as s:
        mv = extended_movers(s, DAY)
        # both AAA(today +0.03) and BBB(-0.05) have a premarket move; BBB is bigger |.|
        assert [m["ticker"] for m in mv["movers"]] == ["BBB", "AAA"]
        bbb = mv["movers"][0]
        assert bbb["pm_pct"] == -0.05 and bbb["premarket_streak"]["direction"] == "loss"
        aaa = next(m for m in mv["movers"] if m["ticker"] == "AAA")
        assert aaa["premarket_streak"] == {"direction": "gain", "count": 3}  # 3rd straight

        hist = extended_history(s, "AAA", days=30)
        assert hist["count"] == 3 and hist["premarket_streak"]["count"] == 3
        assert hist["rows"][0]["date"] == DAY.isoformat()  # newest first


def test_available_dates_helper(engine):
    _seed_history(engine)
    with Session(engine) as s:
        from pipeline.marketdata.extended import available_extended_dates, date_label

        dates = available_extended_dates(s)
        # only sessions with a premarket mover (pm_pct set), newest first
        assert dates == [DAY, DAY - timedelta(days=1), DAY - timedelta(days=2)]
        assert date_label(DAY) == "Fri Jul 24"  # 2026-07-24 is a Friday


def test_endpoints(engine):
    _seed_history(engine)
    client = TestClient(create_app(engine))
    mv = client.get(f"/extended/movers?date={DAY.isoformat()}").json()
    assert mv["date"] == DAY.isoformat() and mv["count"] == 2
    assert mv["movers"][0]["ticker"] == "BBB"
    # available_dates for the selector: all seeded sessions, newest first, labelled
    ads = mv["available_dates"]
    assert [a["date"] for a in ads] == [
        DAY.isoformat(),
        (DAY - timedelta(days=1)).isoformat(),
        (DAY - timedelta(days=2)).isoformat(),
    ]
    assert ads[0]["label"] == "Fri Jul 24"

    # navigating to a past seeded session returns that day's movers
    past = (DAY - timedelta(days=2)).isoformat()
    pm = client.get(f"/extended/movers?date={past}").json()
    assert pm["date"] == past and pm["count"] == 1 and pm["movers"][0]["ticker"] == "AAA"

    # default (no date) -> latest session with data + still carries available_dates
    d0 = client.get("/extended/movers").json()
    assert d0["date"] == DAY.isoformat() and len(d0["available_dates"]) == 3

    h = client.get("/tickers/AAA/extended").json()
    assert h["ticker"] == "AAA" and h["premarket_streak"]["count"] == 3

    # bad date -> 422; empty day -> honest empty (dates list still present)
    assert client.get("/extended/movers?date=nope").status_code == 422
    empty = client.get("/extended/movers?date=2020-01-01").json()
    assert empty["count"] == 0 and empty["movers"] == [] and "available_dates" in empty
