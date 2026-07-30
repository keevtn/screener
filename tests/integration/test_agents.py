"""Phase 7 agent layer: evidence bundles, ranker (parse/retry/persist), spend
logging, the I6/I3 no-config-write invariant, the approval flow, and the force-run
API. No live API calls (the one live smoke is marked and excluded by default).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.agents import (
    LLMResult,
    build_candidate_filter,
    build_evidence_bundle,
    compute_cost,
    resolve_model,
    run_analyst,
    run_ranking,
    select_candidates,
)
from pipeline.agents.client import SoftCapExceeded
from pipeline.api import create_app
from pipeline.common.config import DEFAULT_PARAMS_V1, get_or_create_config
from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    Config,
    LlmSpend,
    PendingChange,
    Prediction,
    Ranking,
    RawItem,
)

NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
CFG = "cfg-test-v1"


# --- fake client -------------------------------------------------------------
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
            input_tokens=120,
            output_tokens=60,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cost_usd=compute_cost(model, input_tokens=120, output_tokens=60),
        )


def _seed(
    session,
    cid,
    ticker,
    *,
    finbert=0.0,
    lm=0.0,
    materiality=0.0,
    catalyst="earnings_results",
    stage="confirmed",
    text_kind="article",
    tier=1,
    published=None,
    high_alert=None,
):
    published = published or (NOW - timedelta(hours=1))
    session.add(
        RawItem(
            id=cid,
            source="Reuters",
            source_class="structured",
            url=f"https://x/{cid}",
            published_at=published,
            ingested_at=published,
            payload_json={"title": f"{ticker} {catalyst}", "guid": cid},
        )
    )
    session.flush()
    session.add(
        Cluster(
            cluster_id=cid,
            origin_item_id=cid,
            member_ids_json=[cid],
            origin_tier=tier,
            member_count=1,
            created_at=published,
        )
    )
    session.add(
        ClusterScore(
            cluster_id=cid,
            finbert_score=finbert,
            lm_score=lm,
            text_kind=text_kind,
            catalyst_type=catalyst,
            event_stage=stage,
            materiality=materiality,
            high_alert=(materiality >= 0.7) if high_alert is None else high_alert,
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


def _rankings_json(items: list[tuple[str, str, float, list[str]]]) -> str:
    return json.dumps(
        {
            "rankings": [
                {
                    "ticker": t,
                    "direction": d,
                    "conviction": c,
                    "rationale": f"{t} looks {d}",
                    "evidence_ids": ev,
                }
                for t, d, c, ev in items
            ]
        }
    )


# --- evidence bundle golden --------------------------------------------------
def test_evidence_bundle_golden(session):
    # age 0 + tier 1 + press_release (0.5/0.5 blend) -> clean window numbers.
    _seed(
        session,
        "c1",
        "AAPL",
        finbert=0.5,
        lm=0.5,
        materiality=0.6,
        text_kind="press_release",
        published=NOW,
    )
    bundle = build_evidence_bundle(session, "AAPL", DEFAULT_PARAMS_V1, CFG, now=NOW)
    assert bundle == {
        "ticker": "AAPL",
        "as_of": NOW.isoformat(),
        "config_version": CFG,
        "window": {
            "sentiment_composite": 0.45,
            "materiality_composite": 0.54,
            "item_count": 1,
            "total_weight": 0.9,
        },
        "clusters": [
            {
                "cluster_id": "c1",
                "published_at": NOW.isoformat(),
                "source": "Reuters",
                "tier": 1,
                "title": "AAPL earnings_results",
                "text_kind": "press_release",
                "finbert_score": 0.5,
                "lm_score": 0.5,
                "catalyst_type": "earnings_results",
                "event_stage": "confirmed",
                "materiality": 0.6,
                "direction_hint": None,
                "high_alert": False,
            }
        ],
    }


# --- candidate filter --------------------------------------------------------
def test_candidate_filter_unions_high_alert_and_sentiment(session):
    _seed(session, "c_ha", "HIGH", materiality=0.8)  # high_alert term
    _seed(session, "c_se", "SENT", finbert=0.7, materiality=0.2)  # extreme_sentiment term
    _seed(session, "c_no", "NONE", finbert=0.1, materiality=0.2)  # matches neither
    tickers = select_candidates(session, build_candidate_filter(), now=NOW)
    assert set(tickers) == {"HIGH", "SENT"}


# --- ranker: parse / retry / persist -----------------------------------------
def test_ranker_parses_valid(session):
    _seed(session, "c1", "AAPL", materiality=0.8)
    _seed(session, "c2", "MSFT", finbert=0.6, materiality=0.2)
    client = FakeClient(
        [_rankings_json([("AAPL", "bullish", 0.8, ["c1"]), ("MSFT", "bearish", 0.5, ["c2"])])]
    )
    run = run_ranking(session, client, params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW)
    assert run.status == "ok"
    assert len(client.calls) == 1
    items = session.execute(select(Ranking).where(Ranking.run_id == run.run_id)).scalars().all()
    assert {i.ticker for i in items} == {"AAPL", "MSFT"}
    # spend logged for the (one) successful call.
    spend = session.execute(select(LlmSpend)).scalars().all()
    assert len(spend) == 1 and spend[0].ok and spend[0].run_id == run.run_id


def test_ranker_rejects_invalid_after_one_retry(session):
    _seed(session, "c1", "AAPL", materiality=0.8)
    client = FakeClient(["not json", "still not json"])
    run = run_ranking(session, client, params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW)
    assert run.status == "failed"
    assert len(client.calls) == 2  # exactly one retry
    assert session.execute(select(Ranking)).scalars().all() == []
    spend = session.execute(select(LlmSpend)).scalars().all()
    assert len(spend) == 2 and all(not s.ok for s in spend)  # both failures logged


def test_ranker_retry_then_succeeds(session):
    _seed(session, "c1", "AAPL", materiality=0.8)
    client = FakeClient(["garbage", _rankings_json([("AAPL", "bullish", 0.7, ["c1"])])])
    run = run_ranking(session, client, params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW)
    assert run.status == "ok" and len(client.calls) == 2
    spend = session.execute(select(LlmSpend).order_by(LlmSpend.created_at)).scalars().all()
    assert [s.ok for s in spend] == [False, True]


def test_ranker_drops_hallucinated_ticker_and_bad_evidence(session):
    _seed(session, "c1", "AAPL", materiality=0.8)
    client = FakeClient(
        [
            _rankings_json(
                [("AAPL", "bullish", 0.7, ["c1", "ghost"]), ("FAKE", "bullish", 0.9, ["c1"])]
            )
        ]
    )
    run = run_ranking(session, client, params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW)
    items = session.execute(select(Ranking).where(Ranking.run_id == run.run_id)).scalars().all()
    assert len(items) == 1 and items[0].ticker == "AAPL"
    assert items[0].evidence_ids_json == ["c1"]  # unknown 'ghost' id filtered out


def test_ranker_empty_universe(session):
    client = FakeClient([])  # never called
    run = run_ranking(session, client, params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW)
    assert run.status == "empty" and run.candidate_count == 0 and client.calls == []


# --- soft cap ----------------------------------------------------------------
def test_soft_cap_blocks_run(session):
    _seed(session, "c1", "AAPL", materiality=0.8)
    session.add(
        LlmSpend(created_at=NOW, purpose="rank", model="claude-sonnet-5", cost_usd=99.0, ok=True)
    )
    session.flush()
    with pytest.raises(SoftCapExceeded):
        run_ranking(
            session, FakeClient([]), params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW, cap=5.0
        )


# --- I6/I3: the agent layer never writes config ------------------------------
def test_agent_package_has_no_config_write_path():
    agents_dir = Path(__file__).resolve().parents[2] / "src" / "pipeline" / "agents"
    for py in agents_dir.glob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert "get_or_create_config" not in src, f"{py.name} must not mint config (I3)"
        assert "Config(" not in src, f"{py.name} must not construct Config rows (I3)"


# --- approval flow -----------------------------------------------------------
def test_approval_flow_creates_new_version(session):
    from scripts.approve import cmd_approve

    base = get_or_create_config(session)  # cfg v1
    change = PendingChange(
        created_at=NOW,
        base_config_version=base.config_version,
        patch_json={"sentiment_threshold": 0.20},
        rationale="raise the bar",
        report_md="# report",
        status="pending",
    )
    session.add(change)
    session.commit()

    cmd_approve(session, change.id, notes="approved in test")
    session.refresh(change)
    assert change.status == "approved"
    new_cfg = session.get(Config, change.resulting_config_version)
    assert new_cfg is not None and new_cfg.config_version != base.config_version
    assert new_cfg.params_json["sentiment_threshold"] == 0.20

    # a prediction issued after approval references the new version.
    pred = Prediction(
        ticker="AAPL",
        direction="bullish",
        confidence=0.6,
        horizon_trading_days=3,
        threshold=0.02,
        issued_at=NOW,
        config_version=new_cfg.config_version,
        status="open",
    )
    session.add(pred)
    session.commit()
    assert session.get(Prediction, pred.prediction_id).config_version == new_cfg.config_version


def test_analyst_patch_cannot_touch_frozen_contract(session):
    base = get_or_create_config(session)
    client = FakeClient(
        [
            json.dumps(
                {
                    "report_md": "weekly",
                    "rationale": "tweak",
                    "proposed_patch": {"threshold": 0.05, "sentiment_threshold": 0.18},
                }
            )
        ]
    )
    change = run_analyst(
        session, client, base_config_version=base.config_version, params=base.params_json, now=NOW
    )
    # frozen contract path dropped; the allowed knob survives.
    assert change.patch_json == {"sentiment_threshold": 0.18}


# --- cost + model resolution -------------------------------------------------
def test_resolve_model_aliases():
    assert resolve_model("sonnet-5") == "claude-sonnet-5"
    assert resolve_model("opus") == "claude-opus-4-8"
    with pytest.raises(ValueError):
        resolve_model("gpt-4")


def test_compute_cost_uses_price_table():
    # sonnet-5 = (3, 15)/MTok: 1M in + 1M out = 3 + 15 = 18.
    assert compute_cost("claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000) == 18.0
    assert compute_cost("claude-sonnet-5", input_tokens=0, output_tokens=0) == 0.0


# --- force-run API -----------------------------------------------------------
def test_force_run_endpoint(engine):
    from pipeline.common.timeutil import utcnow

    with Session(engine) as s:
        # The HTTP endpoint uses real utcnow() for the candidate recency window
        # (unlike the other tests that pin now=NOW), so seed relative to now —
        # a fixed calendar date silently ages out of the 7-day window over time.
        _seed(s, "c1", "AAPL", materiality=0.8, published=utcnow() - timedelta(hours=1))
        s.commit()
    client = FakeClient([_rankings_json([("AAPL", "bullish", 0.9, ["c1"])])])
    app = create_app(engine, llm_client=client)
    tc = TestClient(app)

    r = tc.post("/agents/rank/run", json={"model": "sonnet-5", "horizon_trading_days": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["model"] == "claude-sonnet-5"
    assert body["horizon_trading_days"] == 5
    assert body["items"][0]["ticker"] == "AAPL"

    assert tc.get("/agents/rankings").json()[0]["run_id"] == body["run_id"]
    assert tc.get(f"/agents/rankings/{body['run_id']}").json()["items"][0]["ticker"] == "AAPL"
    spend = tc.get("/agents/spend").json()
    assert spend["calls"] == 1 and spend["total_usd"] > 0
    models = tc.get("/agents/models").json()
    assert "claude-opus-4-8" in models["models"]


def test_force_run_rejects_bad_model(engine):
    app = create_app(engine, llm_client=FakeClient([]))
    tc = TestClient(app)
    r = tc.post("/agents/rank/run", json={"model": "gpt-4"})
    assert r.status_code == 422


# --- ranking breadth + evidence resolution -----------------------------------
def test_default_ranker_candidates_env(monkeypatch):
    from pipeline.agents import default_ranker_candidates

    monkeypatch.delenv("AGENT_RANKER_CANDIDATES", raising=False)
    assert default_ranker_candidates() == 50  # widened default (was 25)
    monkeypatch.setenv("AGENT_RANKER_CANDIDATES", "80")
    assert default_ranker_candidates() == 80
    monkeypatch.setenv("AGENT_RANKER_CANDIDATES", "junk")
    assert default_ranker_candidates() == 50  # bad value -> default


def test_force_run_limit_cap(engine):
    # breadth is configurable per run; the cap protects the token budget.
    app = create_app(engine, llm_client=FakeClient([]))
    tc = TestClient(app)
    assert tc.post("/agents/rank/run", json={"model": "sonnet-5", "limit": 200}).status_code == 422


def test_resolve_clusters_endpoint(engine):
    with Session(engine) as s:
        _seed(s, "cA", "AAPL", finbert=0.6, materiality=0.8, catalyst="earnings_results")
        _seed(s, "cB", "MSFT", finbert=-0.4, materiality=0.5, catalyst="guidance_change")
        s.commit()
    tc = TestClient(create_app(engine, llm_client=FakeClient([])))

    # cited order preserved (cB before cA); unknown id skipped.
    r = tc.get("/clusters/resolve?ids=cB,cA,cMISSING").json()
    assert r["count"] == 2
    assert [i["cluster_id"] for i in r["items"]] == ["cB", "cA"]
    first = r["items"][0]
    assert first["catalyst_type"] == "guidance_change"
    assert first["finbert_score"] == -0.4
    assert first["title"] == "MSFT guidance_change"
    assert first["url"] == "https://x/cB"
    assert first["tickers"] == ["MSFT"]

    # empty query is a clean no-op.
    assert tc.get("/clusters/resolve?ids=").json() == {"count": 0, "items": []}


# --- config panel API (task 7.3) ---------------------------------------------
def test_spend_surfaces_daily_cap(engine):
    from pipeline.common.timeutil import utcnow

    # created_at must be "today" at TEST runtime (the endpoint sums today's spend);
    # a fixed date here starts failing the day after it's written.
    with Session(engine) as s:
        s.add(
            LlmSpend(id="s1", created_at=utcnow(), purpose="rank", model="m", cost_usd=0.5, ok=True)
        )
        s.commit()
    tc = TestClient(create_app(engine, llm_client=FakeClient([])))
    spend = tc.get("/agents/spend").json()
    assert spend["cap_usd"] == 2.0  # AGENT_DAILY_USD_CAP default
    assert spend["today_usd"] == 0.5
    assert spend["pct_of_cap"] == 0.25  # 0.5 / 2.0


def test_config_current_and_versions(engine):
    with Session(engine) as s:
        base = get_or_create_config(s)
        s.commit()
        base_ver = base.config_version
    tc = TestClient(create_app(engine, llm_client=FakeClient([])))

    cur = tc.get("/config/current").json()
    assert cur["config_version"] == base_ver and cur["is_current"] is True
    # full immutable params blob is surfaced (a couple of contract knobs).
    assert cur["params"]["horizon_trading_days"] == 3
    assert cur["params"]["sentiment_threshold"] == 0.15

    versions = tc.get("/config/versions").json()
    assert [v["config_version"] for v in versions] == [base_ver]
    assert versions[0]["is_current"] is True and versions[0]["from_proposal"] is False

    # per-version detail + 404 for the unknown
    assert tc.get(f"/config/versions/{base_ver}").json()["params"]["threshold"] == 0.02
    assert tc.get("/config/versions/cfg-nope").status_code == 404


def test_config_approve_via_api_mints_version(engine):
    with Session(engine) as s:
        base = get_or_create_config(s)
        change = PendingChange(
            id="pc1",
            created_at=NOW,
            base_config_version=base.config_version,
            patch_json={"sentiment_threshold": 0.22},
            rationale="raise the bar",
            report_md="# Weekly review\n\nRaise the directional gate.",
            status="pending",
        )
        s.add(change)
        s.commit()
        base_ver = base.config_version
    tc = TestClient(create_app(engine, llm_client=FakeClient([])))

    # the proposal queue surfaces the markdown + patch for the reviewer.
    props = tc.get("/agents/proposals?status=pending").json()
    assert props["count"] == 1
    p = props["items"][0]
    assert p["report_md"].startswith("# Weekly review")
    assert p["patch"] == {"sentiment_threshold": 0.22}

    # human clicks approve -> a NEW immutable version off the patched params.
    r = tc.post("/config/proposals/pc1/approve", json={"notes": "looks right"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["config_version"] != base_ver
    assert new["params"]["sentiment_threshold"] == 0.22

    # history now marks the minted version as proposal-derived.
    versions = {v["config_version"]: v for v in tc.get("/config/versions").json()}
    assert versions[new["config_version"]]["from_proposal"] is True

    # re-approving the now-resolved proposal is a 409 (not pending).
    assert tc.post("/config/proposals/pc1/approve", json={}).status_code == 409


def test_config_reject_via_api(engine):
    with Session(engine) as s:
        base = get_or_create_config(s)
        s.add(
            PendingChange(
                id="pc2",
                created_at=NOW,
                base_config_version=base.config_version,
                patch_json={"min_items": 3},
                rationale="too noisy",
                status="pending",
            )
        )
        s.commit()
        base_ver = base.config_version
    tc = TestClient(create_app(engine, llm_client=FakeClient([])))

    # a reason is required (the human gate records why).
    assert tc.post("/config/proposals/pc2/reject", json={"reason": ""}).status_code == 422
    r = tc.post("/config/proposals/pc2/reject", json={"reason": "not enough evidence"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert r.json()["resolved_reason"] == "not enough evidence"

    # no new config version was minted by the rejection.
    assert [v["config_version"] for v in tc.get("/config/versions").json()] == [base_ver]


class ExplodingClient:
    """Simulates a provider/transport failure (billing, auth, network down)."""

    def complete(self, *, system, user, model, max_tokens=2048):
        raise RuntimeError("credit balance is too low to access the Anthropic API")


def test_provider_failure_persists_failed_run(engine):
    """A dead LLM provider must yield a FAILED run carrying the reason — not an
    unhandled 500 (which loses its CORS headers and shows as 'network error')."""
    from pipeline.common.timeutil import utcnow

    with Session(engine) as s:
        _seed(s, "c1", "AAPL", materiality=0.8, published=utcnow() - timedelta(hours=1))
        s.commit()
    app = create_app(engine, llm_client=ExplodingClient())
    tc = TestClient(app)

    r = tc.post("/agents/rank/run", json={"model": "sonnet-5"},
                headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert "credit balance" in body["error"]
    # the response went through the normal path, so CORS headers survive. The
    # default is now API_CORS_ORIGINS="*" (commit 6c0e873), so the wildcard is
    # echoed rather than the request Origin.
    assert r.headers.get("access-control-allow-origin") == "*"
    # and the failed run is on the ledger for the RANK page's recent-runs rail
    assert tc.get("/agents/rankings").json()[0]["status"] == "failed"


def test_unhandled_exception_keeps_cors_headers(engine):
    """The catch-all handler answers inside the middleware stack: JSON detail +
    Access-Control-Allow-Origin even on a genuine 500."""
    app = create_app(engine, llm_client=FakeClient([]))

    @app.get("/_boom")
    def _boom() -> None:
        raise RuntimeError("kaboom")

    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/_boom", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 500
    assert "kaboom" in r.json()["detail"]
    # default API_CORS_ORIGINS="*" (commit 6c0e873) -> wildcard echoed
    assert r.headers.get("access-control-allow-origin") == "*"


# --- live smoke (excluded by default) ----------------------------------------
@pytest.mark.live
def test_live_ranker_smoke(engine):
    from pipeline.agents import AnthropicClient

    with Session(engine) as s:
        _seed(s, "c1", "AAPL", finbert=0.7, materiality=0.85)
        s.commit()
    with Session(engine) as s:
        run = run_ranking(
            s,
            AnthropicClient(),
            params=DEFAULT_PARAMS_V1,
            config_version=CFG,
            model="haiku-4-5",
            now=NOW,
        )
        assert run.status in {"ok", "empty"}


# --- Opus guard on the ranker (2026-07-28) -----------------------------------
def test_ranker_opus_guard_downgrades_automated(session):
    """An AUTOMATED run (explicit_model defaults False) requesting Opus is
    downgraded to the Sonnet default, loudly + with provenance — never silent."""
    _seed(session, "c1", "AAPL", materiality=0.8)
    client = FakeClient([_rankings_json([("AAPL", "bullish", 0.8, ["c1"])])])
    run = run_ranking(
        session, client, params=DEFAULT_PARAMS_V1, config_version=CFG,
        model="claude-opus-4-8", trigger="scheduled_daily", now=NOW,
    )
    assert run.status == "ok"
    assert client.calls[0]["model"] == "claude-sonnet-5"  # the CALL used Sonnet
    assert run.model == "claude-sonnet-5"
    assert run.filter_json.get("opus_downgraded") is True
    assert run.filter_json.get("model_requested") == "claude-opus-4-8"


def test_ranker_opus_guard_manual_passthrough(session):
    """An EXPLICIT selection (force-run / on-demand) runs Opus untouched."""
    _seed(session, "c1", "AAPL", materiality=0.8)
    client = FakeClient([_rankings_json([("AAPL", "bullish", 0.8, ["c1"])])])
    run = run_ranking(
        session, client, params=DEFAULT_PARAMS_V1, config_version=CFG,
        model="claude-opus-4-8", explicit_model=True, now=NOW,
    )
    assert client.calls[0]["model"] == "claude-opus-4-8"  # passthrough
    assert run.model == "claude-opus-4-8"
    assert not (run.filter_json or {}).get("opus_downgraded")


def test_ranker_automated_sonnet_is_untouched(session):
    """The guard only touches Opus — an automated Sonnet run is unchanged."""
    _seed(session, "c1", "AAPL", materiality=0.8)
    client = FakeClient([_rankings_json([("AAPL", "bullish", 0.8, ["c1"])])])
    run = run_ranking(session, client, params=DEFAULT_PARAMS_V1, config_version=CFG, now=NOW)
    assert client.calls[0]["model"] == "claude-sonnet-5"
    assert run.model == "claude-sonnet-5"
    assert not (run.filter_json or {}).get("opus_downgraded")


def test_force_run_opus_passthrough(engine):
    """The force-run endpoint is an explicit selection — Opus is NOT downgraded."""
    from pipeline.common.timeutil import utcnow

    with Session(engine) as s:
        _seed(s, "c1", "AAPL", materiality=0.8, published=utcnow() - timedelta(hours=1))
        s.commit()
    client = FakeClient([_rankings_json([("AAPL", "bullish", 0.9, ["c1"])])])
    tc = TestClient(create_app(engine, llm_client=client))
    r = tc.post("/agents/rank/run", json={"model": "opus-4-8", "horizon_trading_days": 5})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "claude-opus-4-8"  # force-run keeps Opus
    assert client.calls[0]["model"] == "claude-opus-4-8"
