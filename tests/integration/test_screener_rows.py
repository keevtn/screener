"""GET /screener/rows: the NEWS screener's universe-scoped, windowed row set.

Covers aggregation correctness (both-axis sentiment kept SEPARATE, I7), the mentions
window vs the wider catalyst lookback, universe restriction (latest fundamentals
snapshot), window-param bounds, and honest empty states.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pipeline.aggregate.screener import screener_rows
from pipeline.api import create_app
from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    Entity,
    FundamentalsSnapshot,
    RawItem,
)

# A fixed reference so window/lookback math is deterministic. The endpoint uses
# utcnow(); the module function takes now= so these assertions don't race the clock.
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
ASOF = date(2026, 7, 20)


def _add_cluster(
    s: Session,
    cid: str,
    ticker: str,
    *,
    published_at: datetime,
    source: str,
    source_class: str = "structured",
    finbert: float | None = None,
    lm: float | None = None,
    catalyst_type: str | None = None,
    event_stage: str | None = None,
    high_alert: bool = False,
) -> None:
    s.add(
        RawItem(
            id=cid,
            source=source,
            source_class=source_class,
            url=f"https://x/{cid}",
            published_at=published_at,
            ingested_at=published_at,
            payload_json={"title": f"{ticker} story {cid}", "guid": cid},
        )
    )
    s.flush()
    s.add(
        Cluster(
            cluster_id=cid,
            origin_item_id=cid,
            member_ids_json=[cid],
            origin_tier=2,
            member_count=1,
            created_at=published_at,
        )
    )
    if finbert is not None or lm is not None or catalyst_type is not None:
        s.add(
            ClusterScore(
                cluster_id=cid,
                finbert_score=finbert,
                lm_score=lm,
                catalyst_type=catalyst_type,
                event_stage=event_stage,
                materiality=0.5 if catalyst_type else 0.0,
                high_alert=high_alert,
                created_at=published_at,
            )
        )
    s.add(
        ClusterEntity(
            cluster_id=cid,
            ticker=ticker,
            ticker_role="subject",
            match_method="name",
            created_at=published_at,
        )
    )


def _fund(ticker: str, sector: str = "Tech") -> FundamentalsSnapshot:
    return FundamentalsSnapshot(
        ticker=ticker,
        as_of=ASOF,
        provider="finviz",
        market_cap=10_000.0,
        price=50.0,
        change_pct=0.01,
        avg_volume=1000.0,
        short_float=0.05,
        beta=1.1,
        sector=sector,
        industry="Software",
        created_at=NOW,
    )


def _seed(engine) -> None:
    with Session(engine) as s:
        # Universe = latest fundamentals snapshot: AAA, BBB, DDD (NOT CCC).
        for t in ("AAA", "BBB", "DDD"):
            s.add(_fund(t))
        for t, name in [("AAA", "Alpha"), ("BBB", "Beta"), ("CCC", "Gamma"), ("DDD", "Delta")]:
            s.add(Entity(ticker=t, canonical_name=name))

        # AAA: two in-window clusters. finbert on both, lm only on the newer one ->
        # exercises SEPARATE per-axis means/latest (different denominators).
        _add_cluster(
            s,
            "a1",
            "AAA",
            published_at=NOW - timedelta(hours=2),
            source="Reuters",
            finbert=0.8,
            lm=0.4,
        )
        _add_cluster(
            s,
            "a2",
            "AAA",
            published_at=NOW - timedelta(hours=10),
            source="Bloomberg",
            finbert=0.2,
            lm=None,
            catalyst_type="earnings_results",
            event_stage="scheduled",
            high_alert=True,
        )

        # BBB: one in-window non-catalyst cluster + an OLD 'ma' catalyst (outside the
        # 48h mentions window but inside the 30d catalyst lookback) -> last_catalyst
        # must surface from the wider lookback even though catalyst_in_window is False.
        _add_cluster(
            s, "b1", "BBB", published_at=NOW - timedelta(hours=5), source="WSJ", finbert=0.1, lm=0.1
        )
        _add_cluster(
            s,
            "b2",
            "BBB",
            published_at=NOW - timedelta(hours=100),
            source="WSJ",
            finbert=0.9,
            lm=0.5,
            catalyst_type="ma",
            event_stage="announced",
            high_alert=True,
        )

        # CCC: in-window coverage but NOT in the universe snapshot -> excluded.
        _add_cluster(
            s,
            "c1",
            "CCC",
            published_at=NOW - timedelta(hours=3),
            source="Reuters",
            finbert=0.5,
            lm=0.5,
        )

        # DDD: universe member but only OUT-of-window coverage -> excluded (no in-window).
        _add_cluster(
            s,
            "d1",
            "DDD",
            published_at=NOW - timedelta(hours=100),
            source="Reuters",
            finbert=0.5,
            lm=0.5,
        )
        s.commit()


def test_rows_aggregation_and_both_axis_separation(engine):
    _seed(engine)
    with Session(engine) as s:
        res = screener_rows(s, hours=48, now=NOW)
    rows = {r["ticker"]: r for r in res["rows"]}

    # Universe restriction (CCC out) + window bound (DDD out).
    assert res["count"] == 2
    assert set(rows) == {"AAA", "BBB"}
    assert res["window_hours"] == 48

    aaa = rows["AAA"]
    assert aaa["mentions"] == 2 and aaa["sources"] == 2
    # Both axes computed from their OWN non-null sets (I7 — never pre-blended):
    # finbert over {0.8, 0.2} -> 0.5; lm over {0.4} only -> 0.4.
    assert aaa["finbert_mean"] == 0.5 and aaa["finbert_latest"] == 0.8
    assert aaa["lm_mean"] == 0.4 and aaa["lm_latest"] == 0.4
    assert aaa["catalyst_in_window"] is True and aaa["high_alert"] is True
    # Most-recent catalyst is the in-window earnings.
    assert aaa["last_catalyst"]["catalyst_type"] == "earnings_results"
    # Folded overlays present.
    assert aaa["fundamentals"]["sector"] == "Tech"
    assert aaa["stats"] is not None and "n_days" in aaa["stats"]


def test_catalyst_lookback_wider_than_mentions_window(engine):
    _seed(engine)
    with Session(engine) as s:
        res = screener_rows(s, hours=48, now=NOW)
    bbb = {r["ticker"]: r for r in res["rows"]}["BBB"]
    # No catalyst in the 48h window, but the ~100h-old 'ma' surfaces via the lookback.
    assert bbb["mentions"] == 1
    assert bbb["catalyst_in_window"] is False
    assert bbb["last_catalyst"]["catalyst_type"] == "ma"
    assert 99 <= bbb["last_catalyst"]["age_hours"] <= 101


def test_window_hours_narrows_aggregation(engine):
    _seed(engine)
    with Session(engine) as s:
        res = screener_rows(s, hours=6, now=NOW)
    rows = {r["ticker"]: r for r in res["rows"]}
    # At 6h: AAA's 10h cluster drops out (mentions 2 -> 1); the earnings catalyst is
    # no longer in-window, but still the most-recent catalyst via lookback.
    assert rows["AAA"]["mentions"] == 1
    assert rows["AAA"]["catalyst_in_window"] is False
    assert rows["AAA"]["last_catalyst"]["catalyst_type"] == "earnings_results"


def test_empty_states(engine):
    # No fundamentals snapshot at all -> no universe -> honest empty (never 500).
    with Session(engine) as s:
        empty = screener_rows(s, hours=48, now=NOW)
    assert empty == {"window_hours": 48, "count": 0, "rows": []}

    # Universe exists but no in-window coverage -> empty rows.
    with Session(engine) as s:
        s.add(_fund("AAA"))
        _add_cluster(s, "old", "AAA", published_at=NOW - timedelta(days=10), source="Reuters")
        s.commit()
    with Session(engine) as s:
        res = screener_rows(s, hours=48, now=NOW)
    assert res["count"] == 0 and res["rows"] == []


def test_endpoint_shape_and_param_bounds(engine):
    _seed(engine)
    client = TestClient(create_app(engine))
    # Default hours; endpoint uses utcnow() so seed-relative counts aren't asserted
    # here — just the contract shape.
    body = client.get("/screener/rows").json()
    assert body["window_hours"] == 48
    assert "count" in body and isinstance(body["rows"], list)

    # Window bounds: 6..168 inclusive; outside -> 422.
    assert client.get("/screener/rows?hours=6").status_code == 200
    assert client.get("/screener/rows?hours=168").status_code == 200
    assert client.get("/screener/rows?hours=5").status_code == 422
    assert client.get("/screener/rows?hours=200").status_code == 422
