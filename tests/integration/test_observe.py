"""Gate 5c task 5c.1: signal-lab observations — features, fundamentals no-lookahead, novelty."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    FundamentalsSnapshot,
    RawItem,
    SignalObservation,
)
from pipeline.lab.observe import observation_id, observe_scored_clusters

T0 = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)  # 14:00 ET -> during market hours


def _cluster(session, cid, ticker, t0, *, finbert=0.5, materiality=0.6, origin=None):
    session.add(
        RawItem(
            id=cid,
            source="Reuters",
            source_class="structured",
            url=f"https://x/{cid}",
            published_at=t0,
            ingested_at=t0,
            payload_json={"title": cid, "guid": cid, **({"origin": origin} if origin else {})},
        )
    )
    session.flush()
    session.add(
        Cluster(
            cluster_id=cid,
            origin_item_id=cid,
            member_ids_json=[cid],
            origin_tier=2,
            member_count=1,
            created_at=t0,
        )
    )
    session.add(
        ClusterScore(
            cluster_id=cid,
            finbert_score=finbert,
            lm_score=0.1,
            finbert_label="bullish",
            text_kind="article",
            catalyst_type="guidance_change",
            materiality=materiality,
            high_alert=False,
            predictive=True,
            reaction_dependent=False,
            created_at=t0,
        )
    )
    session.add(
        ClusterEntity(
            cluster_id=cid, ticker=ticker, ticker_role="subject", match_method="name", created_at=t0
        )
    )


def test_observe_writes_features_and_novelty(engine):
    with Session(engine) as s:
        _cluster(s, "c1", "AAPL", T0)
        _cluster(s, "c2", "AAPL", T0 + timedelta(days=2))
        _cluster(s, "c3", "AAPL", T0 + timedelta(days=4))
        s.commit()

        n = observe_scored_clusters(s)
        assert n == 3
        obs = s.get(SignalObservation, observation_id("c1", "AAPL"))
        assert obs.ticker == "AAPL"
        assert obs.features_json["finbert_score"] == 0.5
        assert obs.features_json["catalyst_type"] == "guidance_change"
        assert obs.features_json["source_tier"] == 2
        assert obs.features_json["after_hours"] is False  # 14:00 ET
        assert obs.status == "open"

        # Novelty ranks 1,2,3 across the ticker's trailing window.
        ranks = sorted(o.novelty_rank for o in s.execute(select(SignalObservation)).scalars().all())
        assert ranks == [1, 2, 3]


def test_fundamentals_no_lookahead(engine):
    with Session(engine) as s:
        _cluster(s, "c1", "AAPL", T0)
        # A snapshot BEFORE t0 (must be used) and one AFTER t0 (must NOT be used, I12).
        s.add(
            FundamentalsSnapshot(
                ticker="AAPL",
                as_of=date(2025, 3, 1),
                provider="finviz",
                market_cap=3.5e12,
                short_float=0.01,
                beta=1.2,
                created_at=T0,
            )
        )
        s.add(
            FundamentalsSnapshot(
                ticker="AAPL",
                as_of=date(2025, 3, 20),
                provider="finviz",
                market_cap=9.0e12,
                short_float=0.99,
                beta=9.9,
                created_at=T0,
            )
        )
        s.commit()

        observe_scored_clusters(s)
        feats = s.get(SignalObservation, observation_id("c1", "AAPL")).features_json
        assert feats["cap_bucket"] == "mega"  # from the 3/1 snapshot
        assert feats["short_float"] == 0.01  # NOT 0.99 from the future snapshot


def test_backfill_flagged_and_fundamentals_null(engine):
    with Session(engine) as s:
        _cluster(s, "cb", "AAPL", T0, origin="legacy_import_v1")
        s.add(
            FundamentalsSnapshot(
                ticker="AAPL",
                as_of=date(2025, 3, 1),
                provider="finviz",
                market_cap=3.5e12,
                short_float=0.01,
                beta=1.2,
                created_at=T0,
            )
        )
        s.commit()

        observe_scored_clusters(s)
        obs = s.get(SignalObservation, observation_id("cb", "AAPL"))
        assert obs.backfill is True
        # Backfilled observations never fake point-in-time fundamentals.
        assert "cap_bucket" not in obs.features_json


def test_observe_incremental_only_new_and_novelty_across_batches(engine):
    # First sweep observes c1,c2; a later sweep adds c3 — only c3 is (re)processed,
    # but its novelty_rank still reflects the EXISTING c1,c2 neighbors (=3).
    with Session(engine) as s:
        _cluster(s, "c1", "AAPL", T0)
        _cluster(s, "c2", "AAPL", T0 + timedelta(days=2))
        s.commit()
        assert observe_scored_clusters(s) == 2  # both new

        # No new clusters -> incremental pass does nothing.
        assert observe_scored_clusters(s) == 0

        _cluster(s, "c3", "AAPL", T0 + timedelta(days=4))
        s.commit()
        n = observe_scored_clusters(s)
        assert n == 1  # ONLY c3 processed (c1,c2 already observed)
        c3 = s.get(SignalObservation, observation_id("c3", "AAPL"))
        assert c3.novelty_rank == 3  # counts c1,c2 (existing) + itself within 30d
        # An out-of-window earlier neighbor must NOT inflate the rank.
        _cluster(s, "c0", "AAPL", T0 - timedelta(days=90))
        s.commit()
        observe_scored_clusters(s)
        c0 = s.get(SignalObservation, observation_id("c0", "AAPL"))
        assert c0.novelty_rank == 1  # 90d before c1 -> alone in its window


def test_observe_full_recompute_matches_incremental(engine):
    # only_new=False reproduces the classic whole-archive ranks (backfill path).
    with Session(engine) as s:
        _cluster(s, "c1", "AAPL", T0)
        _cluster(s, "c2", "AAPL", T0 + timedelta(days=2))
        _cluster(s, "c3", "AAPL", T0 + timedelta(days=4))
        s.commit()
        assert observe_scored_clusters(s, only_new=False) == 3
        ranks = sorted(o.novelty_rank for o in s.execute(select(SignalObservation)).scalars())
        assert ranks == [1, 2, 3]


def test_observe_idempotent(engine):
    with Session(engine) as s:
        _cluster(s, "c1", "AAPL", T0)
        s.commit()
        observe_scored_clusters(s)
        # Simulate a mark already written, then re-observe: marks/status untouched.
        obs = s.get(SignalObservation, observation_id("c1", "AAPL"))
        obs.marks_json = {"car_1d": 0.01}
        obs.status = "matured"
        s.commit()

        observe_scored_clusters(s)
        assert s.execute(select(func.count()).select_from(SignalObservation)).scalar_one() == 1
        obs = s.get(SignalObservation, observation_id("c1", "AAPL"))
        assert obs.marks_json == {"car_1d": 0.01}  # not clobbered
        assert obs.status == "matured"
