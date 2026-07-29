"""Gate 4 task 4.2: threshold rules, abstain, decay-expiry, ledger emission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.aggregate.window import WindowState
from pipeline.common.config import DEFAULT_PARAMS_V1, get_or_create_config
from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    Prediction,
    RawItem,
)
from pipeline.signal.engine import SignalEngine, evaluate_window

T0 = datetime(2025, 3, 12, 14, 0, tzinfo=UTC)
CFG = "cfg-test"


# --- pure threshold rules ----------------------------------------------------


def _window(s, m=0.0, n=2, ids=None):
    return WindowState("AAPL", s, m, n, abs(s), ids or ["c1", "c2"])


def test_threshold_boundary():
    p = DEFAULT_PARAMS_V1
    thr = p["sentiment_threshold"]
    # Epsilon below threshold -> abstain (None).
    assert evaluate_window(_window(thr - 1e-9), p, config_version=CFG, now=T0) is None
    # At threshold -> a prediction with the contract fields + evidence + version.
    pred = evaluate_window(_window(thr), p, config_version=CFG, now=T0)
    assert pred is not None
    assert pred.direction == "bullish"
    assert pred.horizon_trading_days == p["horizon_trading_days"]
    assert pred.threshold == p["threshold"]
    assert pred.config_version == CFG
    assert pred.evidence["cluster_ids"] == ["c1", "c2"]
    # Negative composite -> bearish.
    assert evaluate_window(_window(-thr), p, config_version=CFG, now=T0).direction == "bearish"


def test_min_items_gate():
    p = DEFAULT_PARAMS_V1
    assert evaluate_window(_window(0.9, n=1), p, config_version=CFG, now=T0) is None


# --- DB-backed engine --------------------------------------------------------


def _seed_cluster(session, cid, ticker, *, finbert, lm, materiality=0.3, published=T0, tier=0):
    session.add(
        RawItem(
            id=cid,
            source="Reuters",
            source_class="structured",
            url=f"https://x/{cid}",
            published_at=published,
            ingested_at=published,
            payload_json={"title": f"news {cid}", "guid": cid},
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
            finbert_label="bullish",
            text_kind="article",
            catalyst_type=None,
            materiality=materiality,
            high_alert=False,
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
            match_method="cashtag",
            created_at=published,
        )
    )


def test_abstain_writes_nothing(engine):
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        # Two weak (near-zero) clusters -> composite below threshold -> abstain.
        _seed_cluster(s, "c1", "AAPL", finbert=0.02, lm=0.0)
        _seed_cluster(s, "c2", "AAPL", finbert=-0.01, lm=0.0)
        s.commit()
        eng = SignalEngine(s, cfg.params_json, cfg.config_version, now=T0)
        assert eng.evaluate("AAPL") is None
        assert s.execute(select(func.count()).select_from(Prediction)).scalar_one() == 0


def test_emits_prediction_with_evidence(engine):
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed_cluster(s, "c1", "AAPL", finbert=0.8, lm=0.6)
        _seed_cluster(s, "c2", "AAPL", finbert=0.7, lm=0.5)
        s.commit()
        eng = SignalEngine(s, cfg.params_json, cfg.config_version, now=T0)
        pred = eng.evaluate("AAPL")
        assert pred is not None
        assert pred.direction == "bullish" and pred.status == "open"
        assert pred.config_version == cfg.config_version
        assert set(pred.evidence_json["cluster_ids"]) == {"c1", "c2"}
        assert 0.0 < pred.confidence <= 1.0


def test_decay_expiry_no_new_prediction(engine):
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed_cluster(s, "c1", "AAPL", finbert=0.8, lm=0.6)
        _seed_cluster(s, "c2", "AAPL", finbert=0.7, lm=0.5)
        s.commit()

        # At issue time the signal is above threshold -> emits.
        assert SignalEngine(s, cfg.params_json, cfg.config_version, now=T0).evaluate("AAPL")

        # ~5.4 days later with NO new news, the decayed signal is below threshold.
        later = T0 + timedelta(hours=130)
        assert (
            SignalEngine(s, cfg.params_json, cfg.config_version, now=later).evaluate("AAPL") is None
        )
