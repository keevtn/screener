"""Gate 5 task 5.4: read-only FastAPI endpoints (schema, pagination, filters, health)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pipeline.api import create_app
from pipeline.common.config import get_or_create_config
from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    Prediction,
    RawItem,
)

ISSUED = datetime(2025, 3, 12, 14, 0, tzinfo=UTC)
GRADED = datetime(2025, 3, 17, 21, 0, tzinfo=UTC)


def _seed(engine):
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        # An old ingested_at so /health shows staleness.
        old = datetime.now(UTC) - timedelta(hours=6)
        s.add(
            RawItem(
                id="r1",
                source="Reuters",
                source_class="structured",
                url="https://x/r1",
                published_at=ISSUED,
                ingested_at=old,
                payload_json={"title": "Apple news", "guid": "r1"},
            )
        )
        s.flush()
        s.add(
            Cluster(
                cluster_id="r1",
                origin_item_id="r1",
                member_ids_json=["r1"],
                origin_tier=2,
                member_count=1,
                created_at=ISSUED,
            )
        )
        s.add(
            ClusterScore(
                cluster_id="r1",
                finbert_score=0.5,
                lm_score=0.2,
                finbert_label="bullish",
                text_kind="article",
                catalyst_type="earnings_results",
                event_stage="scheduled",
                materiality=0.6,
                high_alert=False,
                predictive=True,
                reaction_dependent=True,
                created_at=ISSUED,
            )
        )
        s.add(
            ClusterEntity(
                cluster_id="r1",
                ticker="AAPL",
                ticker_role="subject",
                match_method="name",
                created_at=ISSUED,
            )
        )
        s.add(
            Prediction(
                prediction_id="p-graded",
                ticker="AAPL",
                direction="bullish",
                confidence=0.7,
                horizon_trading_days=3,
                threshold=0.02,
                issued_at=ISSUED,
                config_version=cfg.config_version,
                evidence_json={"cluster_ids": ["r1"]},
                status="graded",
                outcome="correct",
                graded_at=GRADED,
                resolving_close=date(2025, 3, 14),
            )
        )
        s.add(
            Prediction(
                prediction_id="p-open",
                ticker="AAPL",
                direction="bearish",
                confidence=0.6,
                horizon_trading_days=3,
                threshold=0.02,
                issued_at=ISSUED,
                config_version=cfg.config_version,
                evidence_json={},
                status="open",
            )
        )
        s.commit()
        return cfg.config_version


def _client(engine):
    return TestClient(create_app(engine))


def test_predictions_filter_and_paginate(engine):
    _seed(engine)
    client = _client(engine)

    body = client.get("/predictions").json()
    assert body["count"] == 2
    assert {p["prediction_id"] for p in body["items"]} == {"p-graded", "p-open"}

    # Filter by status.
    graded = client.get("/predictions", params={"status": "graded"}).json()
    assert graded["count"] == 1 and graded["items"][0]["outcome"] == "correct"

    # Pagination: limit caps the page but count is the full total.
    page = client.get("/predictions", params={"limit": 1}).json()
    assert page["count"] == 2 and len(page["items"]) == 1

    # graded_at is exposed (drives the LEDGER "newly graded" nav badge); null while open.
    by_id = {p["prediction_id"]: p for p in body["items"]}
    assert by_id["p-graded"]["graded_at"] == "2025-03-17T21:00:00Z"
    assert by_id["p-open"]["graded_at"] is None


def test_predictions_carry_origin_context_and_lane_filter(engine):
    """LEDGER lanes: /predictions LEFT-JOINs the companion prediction_context so each
    row carries its origin-news (source_class / headline / url / source) in one shape,
    and ?source_class= filters to a lane. Context is written by the arm-time backfill
    from the cluster/raw_items join (I8: structured-only, so there is no social lane)."""
    from pipeline.common.prediction_context import backfill_prediction_context

    _seed(engine)
    with Session(engine) as s:
        assert backfill_prediction_context(s) == 1  # only p-graded resolves (p-open has no clusters)

    client = _client(engine)
    by_id = {p["prediction_id"]: p for p in client.get("/predictions").json()["items"]}
    g = by_id["p-graded"]
    assert g["source_class"] == "structured"
    assert g["headline"] == "Apple news"
    assert g["url"] == "https://x/r1"
    assert g["source"] == "Reuters"
    # p-open has empty evidence -> no origin resolvable -> honest nulls, still listed.
    assert by_id["p-open"]["source_class"] is None
    assert by_id["p-open"]["headline"] is None

    # Lane filter: STRUCTURED returns the resolved row; SOCIAL is empty (I8).
    structured = client.get("/predictions", params={"source_class": "structured"}).json()
    assert structured["count"] == 1 and structured["items"][0]["prediction_id"] == "p-graded"
    social = client.get("/predictions", params={"source_class": "social"}).json()
    assert social["count"] == 0 and social["items"] == []
    # Unknown lane value is rejected.
    assert client.get("/predictions", params={"source_class": "bogus"}).status_code == 422


def test_predictions_kind_filters_baselines(engine):
    """The ledger surfaced baseline SHADOWS (always_up/random/momentum) next to the
    real prediction — same ticker/issued_at/headline, differing directions — which
    reads as duplicated corrupt data. ?kind=real hides them (the LEDGER default),
    ?kind=baseline isolates them, and each row self-labels via is_baseline/kind."""
    from pipeline.common.models import Config

    _seed(engine)  # 2 real predictions (p-graded, p-open)
    with Session(engine) as s:
        s.add(
            Config(
                config_version="cfg-base-mom",
                params_json={"baseline": "momentum", "k": 5},
                params_hash="test-baseline-momentum-hash",
                created_at=ISSUED,
                notes="baseline momentum",
            )
        )
        s.add(
            Prediction(
                prediction_id="p-base",
                ticker="AAPL",
                direction="bearish",  # momentum's own call — differs from the real on purpose
                confidence=0.5,
                horizon_trading_days=3,
                threshold=0.02,
                issued_at=ISSUED,
                config_version="cfg-base-mom",
                evidence_json={"baseline": "momentum", "shadows": "p-graded"},
                status="open",
            )
        )
        s.commit()

    client = _client(engine)

    # Default = both (unchanged shape for other callers), but each row is labelled.
    all_body = client.get("/predictions").json()
    assert all_body["count"] == 3
    by_id = {p["prediction_id"]: p for p in all_body["items"]}
    assert by_id["p-base"]["is_baseline"] is True
    assert by_id["p-base"]["baseline_kind"] == "momentum"
    assert by_id["p-graded"]["is_baseline"] is False

    # kind=real hides baselines with an accurate count (the LEDGER default).
    real = client.get("/predictions", params={"kind": "real"}).json()
    assert real["count"] == 2
    assert {p["prediction_id"] for p in real["items"]} == {"p-graded", "p-open"}
    assert all(not p["is_baseline"] for p in real["items"])

    # kind=baseline isolates the measurement machinery.
    base = client.get("/predictions", params={"kind": "baseline"}).json()
    assert base["count"] == 1 and base["items"][0]["prediction_id"] == "p-base"

    assert client.get("/predictions", params={"kind": "bogus"}).status_code == 422


def test_intraday_live_degrades_without_redis(engine, monkeypatch):
    # Redis down -> live:false + empty items; the panel falls back to client bucketing.
    import pipeline.common.events as ev

    monkeypatch.setattr(ev, "get_redis", lambda: None)
    client = _client(engine)
    r = client.get("/tickers/aapl/intraday/live").json()
    assert r == {"ticker": "AAPL", "live": False, "items": []}


def test_metrics_endpoint(engine):
    cv = _seed(engine)
    metrics = _client(engine).get("/metrics").json()
    mine = next(m for m in metrics if m["config_version"] == cv)
    assert mine["total_graded"] == 1 and mine["correct"] == 1
    assert mine["hit_rate"] == 1.0


def test_cluster_endpoint_and_404(engine):
    _seed(engine)
    client = _client(engine)
    c = client.get("/clusters/r1").json()
    assert c["origin_source"] == "Reuters"
    assert c["catalyst_type"] == "earnings_results"
    assert c["entities"][0]["ticker"] == "AAPL"
    assert client.get("/clusters/does-not-exist").status_code == 404


def test_ticker_state(engine):
    _seed(engine)
    state = _client(engine).get("/tickers/AAPL/state").json()
    assert state["attributed_clusters"] == 1
    assert "r1" in state["recent_clusters"]
    assert [p["prediction_id"] for p in state["open_predictions"]] == ["p-open"]


def test_health_reflects_staleness(engine):
    _seed(engine)
    h = _client(engine).get("/health").json()
    assert h["raw_items"] == 1 and h["clusters"] == 1 and h["predictions"] == 2
    assert h["last_ingested_at"] is not None
    assert h["staleness_seconds"] > 3600  # ~6h old fixture feed
    # Firehose liveness is always reported (present:false when no heartbeat file).
    assert isinstance(h["firehose"], dict) and "present" in h["firehose"] and "alive" in h["firehose"]


def test_screener_stats_vs_own_history(engine, monkeypatch):
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from pipeline.common.models import AttentionDaily, BuzzBaseline

    now = _dt.now(_UTC)
    today = now.date()
    with Session(engine) as s:
        # RICH: 10 days history averaging 4 mentions/day, sentiment mean ~0.10
        for i in range(1, 11):
            s.add(AttentionDaily(ticker="RICH", date=today - _td(days=i), struct_count=3,
                                 social_count=1, sentiment_mean=0.10, updated_at=now))
        # today: 12 mentions (3x normal), sentiment -0.50 (well below its history)
        s.add(AttentionDaily(ticker="RICH", date=today, struct_count=10, social_count=2,
                             sentiment_mean=-0.50, updated_at=now))
        # POOR: only 2 days history -> ratios must be null, not fabricated
        for i in range(1, 3):
            s.add(AttentionDaily(ticker="POOR", date=today - _td(days=i), struct_count=1,
                                 social_count=0, sentiment_mean=0.2, updated_at=now))
        s.add(BuzzBaseline(ticker="RICH", mean=1.0, std=0.5, n_days=10,
                           source="warm_start", updated_at=now))
        s.commit()

    client = _client(engine)
    body = client.get("/screener/stats?tickers=RICH,POOR,GHOST").json()
    rich, poor, ghost = body["stats"]["RICH"], body["stats"]["POOR"], body["stats"]["GHOST"]

    assert rich["n_days"] == 10 and rich["avg_daily_mentions"] == 4.0
    assert rich["mentions_today"] == 12 and rich["mentions_x_normal"] == 3.0
    assert rich["sent_today"] == -0.5
    # history sentiment is constant 0.10 -> std 0 -> z honestly null (no /0 fake)
    assert rich["sent_z"] is None
    assert rich["buzz_baseline"]["mean"] == 1.0

    assert poor["n_days"] == 2 and poor["mentions_x_normal"] is None
    assert ghost["n_days"] == 0 and ghost["avg_daily_mentions"] is None


def test_screener_stats_search_z(engine):
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from pipeline.common.models import SearchInterestDaily

    now = _dt.now(_UTC)
    today = now.date()
    hist = [10, 12, 8, 11, 9, 10, 13, 7, 10, 11, 9, 12]  # 12 own-history days
    with Session(engine) as s:
        for i, v in enumerate(hist, start=1):
            s.add(SearchInterestDaily(ticker="SRCH", date=today - _td(days=i),
                                      interest=float(v), term="SRCH stock", updated_at=now))
        s.add(SearchInterestDaily(ticker="SRCH", date=today, interest=45.0,  # today spike
                                  term="SRCH stock", updated_at=now))
        s.commit()

    body = _client(engine).get("/screener/stats?tickers=SRCH,GHOST").json()
    srch, ghost = body["stats"]["SRCH"], body["stats"]["GHOST"]
    assert srch["search_z"] is not None and srch["search_z"] > 5  # anomalous vs own history
    assert srch["search_today"] == 45.0 and srch["search_days"] == 12
    assert ghost["search_z"] is None and ghost["search_days"] == 0  # honest null, no history


def test_search_interest_hourly_endpoint(engine, monkeypatch):
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from pipeline.ingest import trends as trends_mod

    monkeypatch.setenv("SEARCH_TRENDS_ENABLED", "1")
    trends_mod._hourly_cache.clear()  # module cache is process-wide; isolate this test
    series = [(_dt(2026, 7, 20, h % 24, 0, tzinfo=_UTC), float(h * 3)) for h in range(10)]

    class _FakeHourly:
        def interest_hourly(self, term, *, timeframe="now 7-d"):
            return 200, series

    app = create_app(engine)
    app.state.trends_client = _FakeHourly()  # inject; endpoint reuses app.state client
    body = TestClient(app).get("/tickers/nvda/search-interest/hourly?hours=6").json()
    assert body["ticker"] == "NVDA" and body["source"] == "google_trends"
    assert body["label"] == "relative interest (0-100, own-term)"
    assert len(body["points"]) == 6 and body["points"][-1]["value"] == 27.0  # tail slice


def test_search_interest_hourly_disabled(engine, monkeypatch):
    monkeypatch.setenv("SEARCH_TRENDS_ENABLED", "0")  # kill-switch -> honest unavailable
    body = TestClient(create_app(engine)).get("/tickers/AAPL/search-interest/hourly").json()
    assert body["source"] == "unavailable" and body["points"] == [] and body["note"] == "disabled"


def test_sim_daily_report_cards(engine):
    from datetime import UTC as _UTC
    from datetime import date as _date
    from datetime import datetime as _dt

    from pipeline.common.models import SimDailySummary

    now = _dt.now(_UTC)
    today = now.date()
    with Session(engine) as s:
        s.add(SimDailySummary(session_date=today, config_id="cfgA", config_name="exp-a",
                              trades=3, open_eod=0, wins=2, losses=1, hit_rate=0.6667,
                              mean_net=0.013, sum_net=0.04, pnl_dollars=40.0, spy_ref=500.0,
                              gate_ref="exp", updated_at=now))
        s.add(SimDailySummary(session_date=_date(2020, 1, 1), config_id="cfgA", config_name="exp-a",
                              trades=1, open_eod=0, wins=0, losses=1, hit_rate=0.0,
                              mean_net=-0.02, sum_net=-0.02, pnl_dollars=-20.0, spy_ref=None,
                              gate_ref="exp", updated_at=now))  # old -> outside the default window
        s.commit()

    client = _client(engine)
    body = client.get("/sim/daily?days=14").json()
    assert body["count"] == 1  # only today's row is inside the 14-day window
    row = body["items"][0]
    assert row["config_name"] == "exp-a" and row["trades"] == 3 and row["pnl_dollars"] == 40.0
    assert row["hit_rate"] == 0.6667 and row["session_date"] == today.isoformat()
    # config filter + a non-matching filter returns empty (never 500)
    assert client.get("/sim/daily?config_id=nope").json()["count"] == 0
