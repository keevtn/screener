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


def _add_pred(session, pid, cv, evidence, issued_at=T0, ticker="AAPL"):
    session.add(
        Prediction(
            prediction_id=pid,
            ticker=ticker,
            direction="bullish",
            confidence=0.7,
            horizon_trading_days=3,
            threshold=0.02,
            issued_at=issued_at,
            config_version=cv,
            evidence_json=evidence,
            status="open",
        )
    )


def test_context_resolves_double_encoded_evidence(engine):
    """A prediction whose evidence_json was double-encoded (stored as a JSON *string*,
    not an object) must still resolve — the live 06:32 rows showed null context with
    no error log, the signature of the dict-guard silently dropping a string. The
    resolver coerces the string back to a dict."""
    from pipeline.common.config import get_or_create_config

    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed(s, "good1", "AAPL", 0.8, 0.6)
        # evidence_json is a Python str -> the JSON column stores it double-encoded,
        # and reads it back as the string '{"cluster_ids": ["good1"]}'.
        _add_pred(s, "p-str", cfg.config_version, '{"cluster_ids": ["good1"]}')
        s.commit()

        assert backfill_prediction_context(s) == 1
        ctx = s.get(PredictionContext, "p-str")
        assert ctx is not None and ctx.source_class == "structured"
        assert ctx.headline == "good1"


def test_context_sentinel_for_aged_unresolvable_only(engine):
    """An AGED prediction whose origin can't resolve gets an all-null sentinel (so it
    stops looping and shows an honest '—'); a FRESH one is left alone for retries."""
    from datetime import UTC, datetime

    from pipeline.common.config import get_or_create_config

    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _add_pred(s, "p-old", cfg.config_version, {"cluster_ids": ["gone"]}, issued_at=T0)
        _add_pred(
            s,
            "p-new",
            cfg.config_version,
            {"cluster_ids": ["gone"]},
            issued_at=datetime.now(UTC),  # too young to sentinel
        )
        s.commit()

        backfill_prediction_context(s)
        old = s.get(PredictionContext, "p-old")
        assert old is not None and old.source_class is None  # sentinel written
        assert old.cluster_id == "gone"  # what it tried, for traceability
        assert s.get(PredictionContext, "p-new") is None  # fresh -> no row yet


def test_context_repair_reresolves_null_row(engine):
    """A null-field context row whose evidence now resolves is repaired in place —
    the one-time recovery for rows stamped empty before their origin was joinable."""
    from datetime import UTC, datetime

    from pipeline.common.config import get_or_create_config
    from pipeline.common.models import PredictionContext as PC
    from pipeline.common.prediction_context import repair_null_context

    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed(s, "good1", "AAPL", 0.8, 0.6)  # now resolvable
        _add_pred(s, "p-null", cfg.config_version, {"cluster_ids": ["good1"]})
        # a pre-existing all-null (sentinel-shaped) row for it
        s.add(PC(prediction_id="p-null", source_class=None, created_at=datetime.now(UTC)))
        s.commit()

        assert repair_null_context(s) == 1
        row = s.get(PC, "p-null")
        assert row.source_class == "structured" and row.headline == "good1"


def test_context_debug_endpoint_shape(engine):
    """The read-only debug helper exposes the stored evidence type, context-row state,
    anti-join eligibility, and a dry-run resolve."""
    from pipeline.common.config import get_or_create_config
    from pipeline.common.prediction_context import resolve_debug

    with Session(engine) as s:
        cfg = get_or_create_config(s)
        _seed(s, "good1", "AAPL", 0.8, 0.6)
        _add_pred(s, "p-dbg", cfg.config_version, {"cluster_ids": ["good1"]})
        s.commit()

        d = resolve_debug(s, "p-dbg")
        assert d["prediction_exists"] is True
        assert d["evidence"]["python_type"] == "dict"
        assert d["context_row_exists"] is False
        assert d["anti_join_selects_as_missing"] is True
        assert d["dry_run_resolve"]["extracted_cluster_ids"] == ["good1"]
        assert d["dry_run_resolve"]["resolved_ctx"]["source_class"] == "structured"
        assert d["dry_run_resolve"]["exception"] is None

        assert resolve_debug(s, "nope")["prediction_exists"] is False


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
