"""Gate 4 task 4.4: signal cycle hook — emit, alert, and cooldown de-dup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.config import get_or_create_config
from pipeline.common.models import Cluster, ClusterEntity, ClusterScore, Prediction, RawItem
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
