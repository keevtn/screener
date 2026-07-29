"""News archive: by-date raw_items feed (day bounds, pagination, filters, enrichment)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pipeline.aggregate.news import news_archive, news_archive_dates
from pipeline.api import create_app
from pipeline.common.models import Cluster, ClusterEntity, ClusterScore, RawItem

ET = ZoneInfo("America/New_York")
DAY_A = date(2026, 7, 16)
DAY_B = date(2026, 7, 17)
NOW = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)  # deterministic "today" for the dates list


def _et(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).astimezone(UTC)


def _item(s, iid, *, when, source, source_class="structured", title, url="https://x/y"):
    s.add(
        RawItem(
            id=iid,
            source=source,
            source_class=source_class,
            url=url,
            published_at=when,
            ingested_at=when,
            payload_json={"title": title, "guid": iid},
        )
    )


def _seed(engine):
    with Session(engine) as s:
        # DAY_A: 3 structured (09/10/11 ET) + 1 social (12 ET). "a2" is a cluster origin.
        _item(s, "a1", when=_et(DAY_A, 9), source="Reuters", title="Alpha reports earnings")
        _item(s, "a2", when=_et(DAY_A, 10), source="SEC EDGAR", title="Apple 8-K merger filing")
        _item(s, "a3", when=_et(DAY_A, 11), source="Reuters", title="Gamma guidance cut")
        _item(
            s,
            "a4",
            when=_et(DAY_A, 12),
            source="Bluesky",
            source_class="social",
            title="social chatter $AAPL",
        )
        # DAY_B: 1 structured
        _item(s, "b1", when=_et(DAY_B, 9), source="Reuters", title="next day news")
        s.flush()
        # cluster for a2 with score + AAPL attribution (the origin-cluster enrichment)
        s.add(
            Cluster(
                cluster_id="a2",
                origin_item_id="a2",
                member_ids_json=["a2"],
                origin_tier=2,
                member_count=1,
                created_at=_et(DAY_A, 10),
            )
        )
        s.add(
            ClusterScore(
                cluster_id="a2",
                finbert_score=0.72,
                lm_score=0.4,
                catalyst_type="ma",
                event_stage="announced",
                materiality=0.6,
                high_alert=True,
                created_at=_et(DAY_A, 10),
            )
        )
        s.add(
            ClusterEntity(
                cluster_id="a2",
                ticker="AAPL",
                ticker_role="subject",
                match_method="name",
                created_at=_et(DAY_A, 10),
            )
        )
        s.commit()


def test_day_bounds_and_order(engine):
    _seed(engine)
    with Session(engine) as s:
        a = news_archive(s, day=DAY_A)
        assert a["count"] == 4  # only DAY_A items (DAY_B's b1 excluded)
        assert [i["id"] for i in a["items"]] == ["a4", "a3", "a2", "a1"]  # newest first
        b = news_archive(s, day=DAY_B)
        assert b["count"] == 1 and b["items"][0]["id"] == "b1"


def test_origin_cluster_enrichment(engine):
    _seed(engine)
    with Session(engine) as s:
        a2 = next(i for i in news_archive(s, day=DAY_A)["items"] if i["id"] == "a2")
        assert a2["tickers"] == ["AAPL"]
        assert a2["sentiment"] == {"score": 0.72}
        assert a2["catalyst_type"] == "ma" and a2["high_alert"] is True
        assert a2["source_type"] == "sec"  # "SEC EDGAR" -> sec badge
        # a non-origin structured item has no attribution overlay (honest)
        a1 = next(i for i in news_archive(s, day=DAY_A)["items"] if i["id"] == "a1")
        assert a1["tickers"] == [] and a1["sentiment"] is None


def test_pagination(engine):
    _seed(engine)
    with Session(engine) as s:
        p1 = news_archive(s, day=DAY_A, limit=2, offset=0)
        p2 = news_archive(s, day=DAY_A, limit=2, offset=2)
        assert p1["count"] == 4 and [i["id"] for i in p1["items"]] == ["a4", "a3"]
        assert [i["id"] for i in p2["items"]] == ["a2", "a1"]


def test_filters(engine):
    _seed(engine)
    with Session(engine) as s:
        assert news_archive(s, day=DAY_A, lane="social")["count"] == 1
        assert news_archive(s, day=DAY_A, lane="structured")["count"] == 3
        assert news_archive(s, day=DAY_A, source="Reuters")["count"] == 2
        # ticker filter -> items whose origin-cluster is attributed to AAPL (a2)
        tk = news_archive(s, day=DAY_A, ticker="AAPL")
        assert tk["count"] == 1 and tk["items"][0]["id"] == "a2"
        # headline substring
        q = news_archive(s, day=DAY_A, q="guidance")
        assert q["count"] == 1 and q["items"][0]["id"] == "a3"


def test_archive_dates(engine):
    _seed(engine)
    with Session(engine) as s:
        dates = news_archive_dates(s, now=NOW)
        assert dates == [DAY_B, DAY_A]  # newest first, only days with items


def test_endpoints(engine):
    _seed(engine)
    c = TestClient(create_app(engine))
    a = c.get(f"/news/archive?date={DAY_A.isoformat()}").json()
    assert a["count"] == 4 and a["items"][0]["id"] == "a4"
    d = c.get("/news/dates").json()
    assert d["count"] >= 2 and d["dates"][0]["label"].startswith(
        ("Mon", "Tue", "Wed", "Thu", "Fri")
    )
    # bad date / lane -> 422
    assert c.get("/news/archive?date=nope").status_code == 422
    assert c.get(f"/news/archive?date={DAY_A.isoformat()}&lane=weird").status_code == 422


def test_live_news_spans_days_newest_first(engine):
    """The LIVE feed (deploy replacement for the Mongo middleware) is day-unbounded:
    all raw_items newest-first, so DAY_B's b1 leads DAY_A's items."""
    _seed(engine)
    with Session(engine) as s:
        from pipeline.aggregate.news import live_news

        r = live_news(s)
        assert [i["id"] for i in r["items"]] == ["b1", "a4", "a3", "a2", "a1"]
        # same cluster-attributed enrichment as the archive
        a2 = next(i for i in r["items"] if i["id"] == "a2")
        assert a2["tickers"] == ["AAPL"] and a2["sentiment"] == {"score": 0.72}


def test_live_news_source_type_and_ticker(engine):
    _seed(engine)
    with Session(engine) as s:
        from pipeline.aggregate.news import live_news

        # coarse source_type: social -> the one social item; sec/rss derived from source name
        assert [i["id"] for i in live_news(s, source_type="social")["items"]] == ["a4"]
        assert [i["id"] for i in live_news(s, source_type="sec")["items"]] == ["a2"]
        assert [i["id"] for i in live_news(s, source_type="rss")["items"]] == ["b1", "a3", "a1"]
        # ticker narrows to items whose origin-cluster is attributed to it
        assert [i["id"] for i in live_news(s, ticker="AAPL")["items"]] == ["a2"]


def test_live_news_endpoint(engine):
    _seed(engine)
    c = TestClient(create_app(engine))
    j = c.get("/api/news?limit=200").json()
    assert j["items"][0]["id"] == "b1" and len(j["items"]) == 5
    assert c.get("/api/news?source_type=social").json()["count"] == 1
    assert c.get("/api/news?ticker=AAPL").json()["items"][0]["id"] == "a2"
    # bad source_type -> 422
    assert c.get("/api/news?source_type=weird").status_code == 422
