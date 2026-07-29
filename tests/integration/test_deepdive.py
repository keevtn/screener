"""Phase 7 deep dive: own-data evidence assembly, the model call (parse/retry/
persist + cluster-id citation cleaning), the distinct-ticker rate limit, the daily
cap, and the analyze/analysis API. No live calls (the one live smoke is marked and
excluded by default).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.agents import (
    LLMResult,
    assemble_evidence,
    compute_cost,
    deep_dive_rate_status,
    run_deep_dive,
)
from pipeline.agents.client import SoftCapExceeded
from pipeline.agents.deepdive import DEEP_DIVE_MAX_TICKERS, DeepDiveRateLimited
from pipeline.api import create_app
from pipeline.common.config import DEFAULT_PARAMS_V1
from pipeline.common.models import (
    AttentionDaily,
    Cluster,
    ClusterEntity,
    ClusterScore,
    FundamentalsSnapshot,
    LlmSpend,
    Prediction,
    RawItem,
    ScheduledEvent,
    TickerAnalysis,
)

NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
CFG = "cfg-test-v1"


class FakeClient:
    """Returns queued replies; records calls. Never touches the network."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, *, system, user, model, max_tokens=2048):
        self.calls.append({"system": system, "user": user, "model": model})
        text = self._replies.pop(0)
        return LLMResult(
            text=text,
            model=model,
            input_tokens=200,
            output_tokens=90,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cost_usd=compute_cost(model, input_tokens=200, output_tokens=90),
        )


def _seed_cluster(session, cid, ticker, *, finbert=0.5, materiality=0.6, published=None):
    published = published or (NOW - timedelta(hours=1))
    session.add(
        RawItem(
            id=cid,
            source="Reuters",
            source_class="structured",
            url=f"https://x/{cid}",
            published_at=published,
            ingested_at=published,
            payload_json={
                "title": f"{ticker} beats on earnings",
                "description": f"{ticker} reported a strong quarter and raised guidance.",
                "guid": cid,
            },
        )
    )
    session.flush()
    session.add(
        Cluster(
            cluster_id=cid,
            origin_item_id=cid,
            member_ids_json=[cid],
            origin_tier=1,
            member_count=1,
            created_at=published,
        )
    )
    session.add(
        ClusterScore(
            cluster_id=cid,
            finbert_score=finbert,
            lm_score=finbert,
            text_kind="press_release",
            catalyst_type="earnings_results",
            event_stage="confirmed",
            materiality=materiality,
            high_alert=materiality >= 0.7,
            predictive=True,
            reaction_dependent=False,
            created_at=published,
        )
    )
    session.add(
        ClusterEntity(
            cluster_id=cid,
            ticker=ticker,
            ticker_role="subject",
            match_method="name",
            created_at=published,
        )
    )
    session.flush()


def _dd_json(
    *,
    direction="bullish",
    conviction=0.72,
    key_evidence=(("Strong quarter and raised guidance", "c1"),),
    risks=("Thin sample",),
    wwc=("A negative pre-announcement",),
) -> str:
    return json.dumps(
        {
            "thesis": "Momentum looks constructive into the next print.",
            "direction": direction,
            "conviction": conviction,
            "key_evidence": [{"point": p, "cluster_id": c} for p, c in key_evidence],
            "risks": list(risks),
            "what_would_change_my_mind": list(wwc),
        }
    )


# --- evidence assembly -------------------------------------------------------
def test_assemble_evidence_gathers_own_data(session):
    from pipeline.common.config import get_or_create_config

    cfg = get_or_create_config(session)  # a real config row for the prediction FK
    _seed_cluster(session, "c1", "AAPL", published=NOW - timedelta(hours=2))
    session.add(
        AttentionDaily(
            ticker="AAPL",
            date=(NOW - timedelta(days=1)).date(),
            struct_count=4,
            social_count=9,
            sentiment_mean=0.3,
            updated_at=NOW,
        )
    )
    session.add(
        Prediction(
            ticker="AAPL",
            direction="bullish",
            confidence=0.6,
            horizon_trading_days=3,
            threshold=0.02,
            issued_at=NOW - timedelta(days=1),
            config_version=cfg.config_version,
            status="open",
        )
    )
    session.add(
        ScheduledEvent(
            ticker="AAPL",
            catalyst_type="earnings_results",
            event_date=(NOW + timedelta(days=10)).date(),
            source="finviz",
            status="upcoming",
            created_at=NOW,
        )
    )
    session.add(
        FundamentalsSnapshot(
            ticker="AAPL",
            as_of=(NOW - timedelta(days=3)).date(),
            provider="finviz",
            market_cap=3.2e12,
            sector="Technology",
            price=210.0,
            created_at=NOW,
        )
    )
    session.commit()

    ev = assemble_evidence(session, "aapl", DEFAULT_PARAMS_V1, CFG, now=NOW)
    assert ev["is_empty"] is False
    assert ev["ticker"] == "AAPL"
    assert len(ev["clusters"]) == 1
    c = ev["clusters"][0]
    assert c["cluster_id"] == "c1" and c["title"] == "AAPL beats on earnings"
    assert "raised guidance" in c["description"]
    assert ev["window"]["item_count"] == 1
    assert len(ev["attention"]) == 1 and ev["attention"][0]["social"] == 9
    assert len(ev["predictions"]) == 1
    assert ev["next_earnings"] == (NOW + timedelta(days=10)).date().isoformat()
    assert ev["fundamentals"]["sector"] == "Technology"


def test_assemble_evidence_empty(session):
    ev = assemble_evidence(session, "ZZZZ", DEFAULT_PARAMS_V1, CFG, now=NOW)
    assert ev["is_empty"] is True
    assert ev["clusters"] == [] and ev["attention"] == []


# --- model call: parse / persist / citation cleaning -------------------------
def test_run_deep_dive_persists_and_logs_spend(session):
    _seed_cluster(session, "c1", "AAPL")
    client = FakeClient([_dd_json()])
    a = run_deep_dive(
        session, client, "AAPL", params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW
    )
    assert a.status == "ok" and a.direction == "bullish"
    assert a.key_evidence_json[0]["cluster_id"] == "c1"
    assert a.evidence_json["ticker"] == "AAPL"  # snapshot persisted for audit
    spend = session.execute(select(LlmSpend)).scalars().all()
    assert len(spend) == 1 and spend[0].ok and spend[0].purpose == "deep_dive"


def test_run_deep_dive_nulls_unknown_cluster_id(session):
    _seed_cluster(session, "c1", "AAPL")
    client = FakeClient(
        [_dd_json(key_evidence=(("real point", "c1"), ("hallucinated", "ghost")))]
    )
    a = run_deep_dive(
        session, client, "AAPL", params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW
    )
    ids = [e["cluster_id"] for e in a.key_evidence_json]
    assert ids == ["c1", None]  # unknown id nulled, the point kept


def test_run_deep_dive_retry_then_fail(session):
    _seed_cluster(session, "c1", "AAPL")
    client = FakeClient(["not json", "still not json"])
    a = run_deep_dive(
        session, client, "AAPL", params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW
    )
    assert a.status == "failed" and len(client.calls) == 2
    spend = session.execute(select(LlmSpend)).scalars().all()
    assert len(spend) == 2 and all(not s.ok for s in spend)


def test_run_deep_dive_empty_skips_model(session):
    client = FakeClient([])  # never called
    a = run_deep_dive(
        session, client, "ZZZZ", params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW
    )
    assert a.status == "empty" and client.calls == []
    assert session.execute(select(LlmSpend)).scalars().all() == []


# --- rate limit --------------------------------------------------------------
def _insert_analysis(session, ticker, created_at, status="ok"):
    session.add(
        TickerAnalysis(
            ticker=ticker,
            created_at=created_at,
            model="claude-sonnet-5",
            horizon_trading_days=3,
            config_version=CFG,
            status=status,
        )
    )
    session.commit()


def test_rate_status_allows_within_limit_and_reruns(session):
    _insert_analysis(session, "AAA", NOW - timedelta(minutes=1))
    # one distinct ticker so far -> a new distinct ticker still allowed
    allowed, retry = deep_dive_rate_status(session, "BBB", now=NOW)
    assert allowed and retry == 0
    # re-running the SAME ticker is always allowed (adds no new distinct ticker)
    allowed, retry = deep_dive_rate_status(session, "AAA", now=NOW)
    assert allowed and retry == 0


def test_rate_status_blocks_third_distinct(session):
    _insert_analysis(session, "AAA", NOW - timedelta(minutes=1))
    _insert_analysis(session, "BBB", NOW - timedelta(minutes=2))
    assert DEEP_DIVE_MAX_TICKERS == 2
    allowed, retry = deep_dive_rate_status(session, "CCC", now=NOW)
    assert not allowed and 0 < retry <= 300
    # the empty (no-cost) rows don't count toward the limit
    _insert_analysis(session, "DDD", NOW - timedelta(seconds=10), status="empty")
    allowed, _ = deep_dive_rate_status(session, "CCC", now=NOW)
    assert not allowed  # DDD's empty row didn't free/consume anything


def test_run_deep_dive_raises_when_rate_limited(session):
    _seed_cluster(session, "c1", "CCC")
    _insert_analysis(session, "AAA", NOW - timedelta(minutes=1))
    _insert_analysis(session, "BBB", NOW - timedelta(minutes=2))
    with pytest.raises(DeepDiveRateLimited) as exc:
        run_deep_dive(
            session, FakeClient([_dd_json()]), "CCC",
            params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW,
        )
    assert exc.value.retry_after > 0


def test_rate_limit_bypassable_for_ops(session):
    _seed_cluster(session, "c1", "CCC")
    _insert_analysis(session, "AAA", NOW - timedelta(minutes=1))
    _insert_analysis(session, "BBB", NOW - timedelta(minutes=2))
    a = run_deep_dive(
        session, FakeClient([_dd_json()]), "CCC",
        params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW, enforce_rate_limit=False,
    )
    assert a.status == "ok"


# --- daily cap ---------------------------------------------------------------
def test_daily_cap_blocks_run(session):
    _seed_cluster(session, "c1", "AAPL")
    session.add(
        LlmSpend(
            created_at=NOW, purpose="deep_dive", model="claude-sonnet-5", cost_usd=99.0, ok=True
        )
    )
    session.commit()
    with pytest.raises(SoftCapExceeded):
        run_deep_dive(
            session, FakeClient([_dd_json()]), "AAPL",
            params=DEFAULT_PARAMS_V1, config_version=CFG, cap=5.0, now=NOW,
        )


# --- API ---------------------------------------------------------------------
def test_analyze_and_fetch_api(engine):
    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL")
        s.commit()
    client = FakeClient([_dd_json()])
    app = create_app(engine, llm_client=client)
    tc = TestClient(app)

    # nothing yet -> null
    assert tc.get("/tickers/AAPL/analysis").json() is None

    r = tc.post("/tickers/AAPL/analyze", json={"model": "sonnet-5", "horizon_trading_days": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["direction"] == "bullish"
    assert body["horizon_trading_days"] == 5
    assert body["key_evidence"][0]["cluster_id"] == "c1"
    assert body["evidence"]["ticker"] == "AAPL"

    # persisted -> instant revisit returns the same analysis
    latest = tc.get("/tickers/AAPL/analysis").json()
    assert latest["analysis_id"] == body["analysis_id"]
    assert tc.get("/tickers/AAPL/analyses").json()[0]["analysis_id"] == body["analysis_id"]


def test_analyze_rejects_bad_model(engine):
    app = create_app(engine, llm_client=FakeClient([]))
    tc = TestClient(app)
    r = tc.post("/tickers/AAPL/analyze", json={"model": "gpt-4"})
    assert r.status_code == 422


def test_analyze_rate_limit_429_with_retry_after(engine):
    with Session(engine) as s:
        _seed_cluster(s, "c1", "CCC")
        s.add_all(
            [
                TickerAnalysis(
                    ticker="AAA", created_at=datetime.now(UTC), model="claude-sonnet-5",
                    horizon_trading_days=3, config_version=CFG, status="ok",
                ),
                TickerAnalysis(
                    ticker="BBB", created_at=datetime.now(UTC), model="claude-sonnet-5",
                    horizon_trading_days=3, config_version=CFG, status="ok",
                ),
            ]
        )
        s.commit()
    app = create_app(engine, llm_client=FakeClient([_dd_json()]))
    tc = TestClient(app)
    r = tc.post("/tickers/CCC/analyze", json={"model": "sonnet-5"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0

    rate = tc.get("/tickers/CCC/analysis/rate").json()
    assert rate["allowed"] is False and rate["retry_after"] > 0


# --- live smoke (excluded by default) ----------------------------------------
@pytest.mark.live
def test_live_deep_dive_smoke(engine):
    from pipeline.agents import AnthropicClient

    with Session(engine) as s:
        _seed_cluster(s, "c1", "AAPL", finbert=0.7, materiality=0.85)
        s.commit()
    with Session(engine) as s:
        a = run_deep_dive(
            s, AnthropicClient(), "AAPL",
            params=DEFAULT_PARAMS_V1, config_version=CFG, model="haiku-4-5", now=NOW,
        )
        assert a.status in {"ok", "failed"}
