"""Gate 4 task 4.4: signal cycle hook — emit, alert, and cooldown de-dup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.config import get_or_create_config
from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    Prediction,
    PredictionContext,
    RawItem,
)
from pipeline.common.prediction_context import backfill_prediction_context
from pipeline.signal.cycle import run_signal_cycle

T0 = datetime(2025, 3, 12, 14, 0, tzinfo=UTC)


def _seed(session, cid, ticker, finbert, lm):
    session.add(
        RawItem(
            id=cid,
            source="Reuters",
            source_class="structured",
            url=f"https://x/{cid}",
            published_at=T0,
            ingested_at=T0,
            payload_json={"title": cid, "guid": cid},
        )
    )
    session.flush()
    session.add(
        Cluster(
            cluster_id=cid,
            origin_item_id=cid,
            member_ids_json=[cid],
            origin_tier=0,
            member_count=1,
            created_at=T0,
        )
    )
    session.add(
        ClusterScore(
            cluster_id=cid,
            finbert_score=finbert,
            lm_score=lm,
            finbert_label="bullish",
            text_kind="article",
            materiality=0.3,
            high_alert=False,
            predictive=True,
            reaction_dependent=False,
            created_at=T0,
        )
    )
    session.add(
        ClusterEntity(
            cluster_id=cid,
            ticker=ticker,
            ticker_role="subject",
            match_method="cashtag",
            created_at=T0,
        )
    )


def test_cycle_emits_and_alerts(engine):
    fired: list[str] = []
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed(s, "c1", "AAPL", 0.8, 0.6)
        _seed(s, "c2", "AAPL", 0.7, 0.5)
        s.commit()

        preds = run_signal_cycle(
            s, cfg.params_json, cfg.config_version, now=T0, alert=lambda p: fired.append(p.ticker)
        )
        assert [p.ticker for p in preds] == ["AAPL"]
        assert fired == ["AAPL"]  # alerted once
        assert s.execute(select(func.count()).select_from(Prediction)).scalar_one() == 1


def test_cycle_arm_then_context_writes_origin_news(engine):
    """Deployed cycle wiring: a prediction armed by run_signal_cycle must receive its
    origin-news context from the same-cycle backfill step (the exact ordering
    scripts/run_pipeline.py runs: signal -> context on one shared session). Guards the
    live regression where NEW predictions showed null source_class/headline/url."""
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed(s, "c1", "AAPL", 0.8, 0.6)
        _seed(s, "c2", "AAPL", 0.7, 0.5)
        s.commit()

        preds = run_signal_cycle(
            s, cfg.params_json, cfg.config_version, now=T0, alert=lambda p: None
        )
        assert len(preds) == 1
        pid = preds[0].prediction_id

        # The context step runs on the same session immediately after signal.
        written = backfill_prediction_context(s)
        assert written == 1

        ctx = s.get(PredictionContext, pid)
        assert ctx is not None, "armed prediction did not receive origin-news context"
        assert ctx.source_class == "structured"
        assert ctx.headline in {"c1", "c2"}  # origin is one of the contributing clusters
        assert ctx.url and ctx.url.startswith("https://x/")

        # Idempotent: a second pass writes nothing (no duplicate rows).
        assert backfill_prediction_context(s) == 0


def test_context_backfill_resilient_to_bad_evidence(engine):
    """A single prediction with malformed evidence_json (a non-dict) must NOT abort
    the whole backfill — otherwise one bad row silently starves every other new
    prediction of origin-news context, every cycle (the live 2026-07-30 regression).
    The good rows still get context; the bad one is skipped."""
    from pipeline.common.config import get_or_create_config

    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed(s, "good1", "AAPL", 0.8, 0.6)  # a resolvable structured origin
        # A real prediction pointing at the good cluster.
        s.add(
            Prediction(
                prediction_id="p-good",
                ticker="AAPL",
                direction="bullish",
                confidence=0.7,
                horizon_trading_days=3,
                threshold=0.02,
                issued_at=T0,
                config_version=cfg.config_version,
                evidence_json={"cluster_ids": ["good1"]},
                status="open",
            )
        )
        # A prediction whose evidence_json is a LIST, not a dict — the poison row.
        s.add(
            Prediction(
                prediction_id="p-bad",
                ticker="AAPL",
                direction="bullish",
                confidence=0.7,
                horizon_trading_days=3,
                threshold=0.02,
                issued_at=T0,
                config_version=cfg.config_version,
                evidence_json=["not", "a", "dict"],
                status="open",
            )
        )
        s.commit()

        written = backfill_prediction_context(s)  # must not raise
        assert written == 1  # the good row landed; the bad one skipped

        good = s.get(PredictionContext, "p-good")
        assert good is not None and good.source_class == "structured"
        assert s.get(PredictionContext, "p-bad") is None  # unresolvable -> no row, no crash


def test_cooldown_prevents_reemit(engine):
    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed(s, "c1", "AAPL", 0.8, 0.6)
        _seed(s, "c2", "AAPL", 0.7, 0.5)
        s.commit()

        first = run_signal_cycle(
            s, cfg.params_json, cfg.config_version, now=T0, alert=lambda p: None
        )
        assert len(first) == 1

        # A cycle 1h later: within cooldown -> no new prediction (no ledger spam).
        again = run_signal_cycle(
            s,
            cfg.params_json,
            cfg.config_version,
            now=T0 + timedelta(hours=1),
            alert=lambda p: None,
        )
        assert again == []
        assert s.execute(select(func.count()).select_from(Prediction)).scalar_one() == 1
