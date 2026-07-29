"""Attention-baseline layer: daily rollup, warm-start buzz baselines, series API."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from pipeline.aggregate.attention import (
    build_attention_daily,
    buzz_z,
    compute_buzz_baselines,
)
from pipeline.common.models import (
    AttentionDaily,
    BuzzBaseline,
    Cluster,
    ClusterEntity,
    ClusterScore,
    RawItem,
)


def _add(s, cid, ticker, d: date, source_class: str, finbert: float):
    pub = datetime(d.year, d.month, d.day, 14, 0, tzinfo=UTC)
    s.add(
        RawItem(
            id=cid,
            source="X",
            source_class=source_class,
            url=f"u/{cid}",
            published_at=pub,
            ingested_at=pub,
            payload_json={},
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
            created_at=pub,
        )
    )
    s.add(
        ClusterScore(
            cluster_id=cid,
            finbert_score=finbert,
            lm_score=0.0,
            text_kind="article",
            materiality=0.5,
            created_at=pub,
        )
    )
    s.add(
        ClusterEntity(
            cluster_id=cid,
            ticker=ticker,
            ticker_role="subject",
            match_method="cashtag",
            created_at=pub,
        )
    )


def _seed(engine):
    with Session(engine) as s:
        n = 0
        # AAA: social volume across 5 days [1,2,5,1,3] + some structured
        plan = {date(2026, 6, 1): 1, date(2026, 6, 2): 2, date(2026, 6, 3): 5,
                date(2026, 6, 4): 1, date(2026, 6, 5): 3}
        for d, k in plan.items():
            for _ in range(k):
                _add(s, f"s{n}", "AAA", d, "social", 0.4)
                n += 1
            _add(s, f"t{n}", "AAA", d, "structured", -0.2)  # one structured/day
            n += 1
        # BBB: only 1 social day -> below min_days, no baseline
        _add(s, f"s{n}", "BBB", date(2026, 6, 3), "social", 0.1)
        s.commit()


def test_attention_rollup(engine):
    _seed(engine)
    with Session(engine) as s:
        rows = build_attention_daily(s, [engine])
        assert rows > 0
        a = s.get(AttentionDaily, ("AAA", date(2026, 6, 3)))
        assert a.social_count == 5 and a.struct_count == 1
        # sentiment_mean blends the day's finbert (5 social @0.4 + 1 struct @-0.2)
        assert abs(a.sentiment_mean - (5 * 0.4 - 0.2) / 6) < 1e-6


def test_buzz_baseline_and_z(engine):
    _seed(engine)
    with Session(engine) as s:
        build_attention_daily(s, [engine])
        n = compute_buzz_baselines(s, min_days=3, shrink_k=0)  # no shrink -> raw stats
        assert n == 1  # AAA qualifies (5 days); BBB (1 day) does not
        base = s.get(BuzzBaseline, "AAA")
        assert base is not None and base.n_days == 5
        assert s.get(BuzzBaseline, "BBB") is None
        # a high-social day is above baseline -> positive z; a quiet day negative
        assert buzz_z(5, base) > 0
        assert buzz_z(1, base) < 0
        assert buzz_z(5, None) is None  # no baseline -> no buzz


def test_ticker_series_endpoint(engine):
    from fastapi.testclient import TestClient

    from pipeline.api import create_app

    _seed(engine)
    with Session(engine) as s:
        build_attention_daily(s, [engine])
        compute_buzz_baselines(s, min_days=3)

    tc = TestClient(create_app(engine))
    b = tc.get("/tickers/AAA/series?days=400").json()
    assert b["ticker"] == "AAA"
    assert b["baseline"] is not None and b["baseline"]["n_days"] == 5
    assert len(b["attention"]) == 5
    peak = next(x for x in b["attention"] if x["date"] == "2026-06-03")
    assert peak["social"] == 5 and peak["buzz_z"] > 0
    # BBB has attention but no baseline -> buzz_z null
    z = tc.get("/tickers/BBB/series").json()
    assert z["baseline"] is None
    assert z["attention"][0]["buzz_z"] is None


def test_buzz_latest_endpoint(engine):
    from fastapi.testclient import TestClient

    from pipeline.api import create_app

    _seed(engine)
    with Session(engine) as s:
        build_attention_daily(s, [engine])
        compute_buzz_baselines(s, min_days=3)

    tc = TestClient(create_app(engine))
    buzz = tc.get("/buzz/latest").json()["buzz"]
    assert "AAA" in buzz and isinstance(buzz["AAA"], (int, float))  # has a baseline
    assert "BBB" not in buzz  # no baseline -> absent (screener renders "—")


def test_ticker_clusters_endpoint(engine):
    from fastapi.testclient import TestClient

    from pipeline.api import create_app

    _seed(engine)  # AAA has structured + social clusters across dates
    tc = TestClient(create_app(engine))
    body = tc.get("/tickers/AAA/clusters?limit=50").json()
    assert body["ticker"] == "AAA" and body["count"] > 0
    it = body["items"][0]  # newest first
    assert {"cluster_id", "title", "source", "source_class", "published_at"} <= set(it)
    assert body["items"][0]["published_at"] >= body["items"][-1]["published_at"]  # desc order
