"""UNIVERSE screener: server-side filter over fundamentals_snapshots + overlays."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pipeline.api import create_app
from pipeline.common.models import Entity, FundamentalsSnapshot, Prediction, ScheduledEvent

NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
ASOF = date(2026, 7, 12)


def _fund(ticker, sector, mcap, price, short, **kw):
    return FundamentalsSnapshot(
        ticker=ticker,
        as_of=ASOF,
        provider="finviz",
        market_cap=mcap,
        price=price,
        short_float=short,
        sector=sector,
        industry=kw.get("industry", "Software"),
        avg_volume=kw.get("avg_volume", 1000.0),
        beta=kw.get("beta", 1.0),
        change_pct=kw.get("change_pct", 0.01),
        created_at=NOW,
    )


def _seed(engine):
    with Session(engine) as s:
        for t, name in [("AAA", "Alpha"), ("BBB", "Beta"), ("CCC", "Gamma")]:
            s.add(Entity(ticker=t, canonical_name=name))
        s.add(_fund("AAA", "Tech", 500_000.0, 100.0, 0.30))
        s.add(_fund("BBB", "Tech", 50_000.0, 20.0, 0.05))
        s.add(_fund("CCC", "Healthcare", 800_000.0, 250.0, 0.02, industry="Biotech"))
        s.add(
            Prediction(
                ticker="AAA",
                direction="bullish",
                confidence=0.7,
                horizon_trading_days=3,
                threshold=0.02,
                issued_at=NOW,
                config_version="cfg-x",
                status="open",
            )
        )
        s.add(
            ScheduledEvent(
                ticker="AAA",
                catalyst_type="earnings_results",
                event_date=date(2026, 7, 20),
                source="finviz",
                status="upcoming",
                created_at=NOW,
            )
        )
        # a config row for the prediction FK
        from pipeline.common.models import Config

        s.add(
            Config(config_version="cfg-x", params_json={}, params_hash="x", created_at=NOW)
        )
        s.commit()


def test_universe_facets(engine):
    _seed(engine)
    tc = TestClient(create_app(engine))
    f = tc.get("/universe/facets").json()
    assert f["universe"] == 3
    assert {s["name"] for s in f["sectors"]} == {"Tech", "Healthcare"}
    assert "Biotech" in f["industries"]


def test_universe_filters_and_overlays(engine):
    _seed(engine)
    tc = TestClient(create_app(engine))

    # sector filter
    tech = tc.get("/universe/screen?sector=Tech").json()
    assert tech["count"] == 2
    assert {i["ticker"] for i in tech["items"]} == {"AAA", "BBB"}

    # market-cap filter + sort
    big = tc.get("/universe/screen?mcap_min=100000&sort=market_cap&order=desc").json()
    assert [i["ticker"] for i in big["items"]] == ["CCC", "AAA"]  # 800k, 500k

    # short-float (squeeze) filter
    sq = tc.get("/universe/screen?short_min=0.20").json()
    assert sq["count"] == 1 and sq["items"][0]["ticker"] == "AAA"

    # overlays: AAA has a live signal + an upcoming earnings date
    aaa = next(i for i in tech["items"] if i["ticker"] == "AAA")
    assert aaa["signal"] == {"direction": "bullish", "confidence": 0.7}
    assert aaa["next_earnings"] == "2026-07-20"
    assert aaa["name"] == "Alpha"

    # has_signal filter narrows to AAA only
    sig = tc.get("/universe/screen?has_signal=true").json()
    assert {i["ticker"] for i in sig["items"]} == {"AAA"}


def test_fundamentals_bulk_overlay(engine):
    _seed(engine)
    tc = TestClient(create_app(engine))
    b = tc.get("/fundamentals?tickers=AAA,CCC,NOPE").json()
    assert b["count"] == 2  # NOPE has no snapshot -> absent
    assert b["fundamentals"]["AAA"]["sector"] == "Tech"
    assert b["fundamentals"]["AAA"]["name"] == "Alpha"  # Entity join (screener search)
    assert b["fundamentals"]["CCC"]["market_cap"] == 800_000.0
    assert "NOPE" not in b["fundamentals"]


def test_universe_search(engine):
    _seed(engine)
    # A tiny ticker whose symbol is a prefix of AAA and a substring of every
    # seeded name ("a") — exercises exact-first ranking under mcap-desc sort.
    with Session(engine) as s:
        s.add(Entity(ticker="A", canonical_name="Ayy Industries"))
        s.add(_fund("A", "Tech", 10.0, 1.0, 0.01))
        s.commit()
    tc = TestClient(create_app(engine))

    # Symbol prefix match.
    r = tc.get("/universe/screen?q=BB").json()
    assert r["count"] == 1 and r["items"][0]["ticker"] == "BBB"

    # Company-name substring match, case-insensitive (CCC is "Gamma").
    r = tc.get("/universe/screen?q=gamm").json()
    assert r["count"] == 1 and r["items"][0]["ticker"] == "CCC"

    # "a" matches A + AAA by prefix and Alpha/Beta/Gamma/Ayy by name substring,
    # but the exact symbol outranks the mcap-desc sort (A is the smallest cap).
    r = tc.get("/universe/screen?q=a&sort=market_cap&order=desc").json()
    assert r["count"] == 4
    assert r["items"][0]["ticker"] == "A"
    assert [i["ticker"] for i in r["items"][1:]] == ["CCC", "AAA", "BBB"]  # mcap desc

    # AND-composes with the other filters.
    r = tc.get("/universe/screen?q=a&sector=Healthcare").json()
    assert [i["ticker"] for i in r["items"]] == ["CCC"]

    # No match -> honest empty, count 0.
    r = tc.get("/universe/screen?q=zzz").json()
    assert r["count"] == 0 and r["items"] == []


def test_universe_pagination(engine):
    _seed(engine)
    tc = TestClient(create_app(engine))
    p1 = tc.get("/universe/screen?sort=market_cap&order=desc&limit=1&offset=0").json()
    p2 = tc.get("/universe/screen?sort=market_cap&order=desc&limit=1&offset=1").json()
    assert p1["count"] == 3 and len(p1["items"]) == 1
    assert p1["items"][0]["ticker"] == "CCC" and p2["items"][0]["ticker"] == "AAA"
