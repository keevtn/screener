"""Gate 3 tasks 3.1/3.3/3.4: sentiment axis, routing, earnings guard, scored-once."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.models import Cluster, ClusterScore, RawItem
from pipeline.enrich.canonical import from_values as canonical
from pipeline.enrich.tiers import load_source_tiers
from pipeline.score.routing import text_kind_of
from pipeline.score.score import score_cluster, score_clusters
from pipeline.score.sentiment import default_lm, score_sentiment

BASE = datetime(2025, 3, 12, 14, 0, tzinfo=UTC)


@dataclass
class _R:
    score: float
    label: str
    confidence: float = 0.9


class FakeFinBERT:
    def __init__(self, score=0.3, label="bullish"):
        self._r = _R(score, label)

    def analyze_text_batch(self, pairs):
        return [self._r for _ in pairs]


# --- 3.1 sentiment: separate fields (I7) ------------------------------------


def test_scores_stored_separately_no_blend():
    cols = {c.name for c in ClusterScore.__table__.columns}
    assert {"finbert_label", "finbert_score", "lm_score"} <= cols
    # No pre-blended sentiment column exists anywhere (I7).
    assert not any(("blend" in c) or c in {"sentiment", "combined_score"} for c in cols)


def test_score_sentiment_keeps_models_separate():
    scores = score_sentiment(
        "Company posts strong profit growth", "", finbert=FakeFinBERT(0.4), lm=default_lm()
    )
    assert scores.finbert_label == "bullish"
    assert scores.finbert_score == 0.4
    assert scores.lm_score is not None  # L-M computed independently
    # Two separate attributes; no single blended field is produced (I7).
    assert hasattr(scores, "finbert_score") and hasattr(scores, "lm_score")


# --- 3.3 routing -------------------------------------------------------------


def test_text_kind_by_tier():
    tiers = load_source_tiers()
    assert text_kind_of("SEC EDGAR — 8-K", tiers) == "filing"
    assert text_kind_of("Business Wire", tiers) == "press_release"
    assert text_kind_of("Reuters", tiers) == "article"


# --- 3.4 earnings-surprise guard --------------------------------------------


def test_guidance_over_results():
    tiers = load_source_tiers()
    from pipeline.score.catalysts import load_taxonomy

    origin = canonical(
        id="e1",
        source="SEC EDGAR — 10-Q",
        source_class="structured",
        title="Acme reports Q3 results, cuts guidance for full year",
        published_at=BASE,
        extra={"filing_type": "10-Q"},
    )
    values = score_cluster(
        "e1", origin, taxonomy=load_taxonomy(), tiers=tiers, finbert=FakeFinBERT(), lm=default_lm()
    )
    assert values.catalyst_type == "earnings_results"
    assert values.reaction_dependent is True
    # Guidance language overrides results-level text direction (bearish).
    assert values.direction_hint == "bearish"


# --- orchestrator: scored once + idempotent ---------------------------------


def _seed(session):
    specs = [
        ("s1", "SEC EDGAR — S-1", "Company files registration statement on Form S-1", "S-1"),
        ("nw", "Reuters", "Markets mixed in afternoon trading", None),
    ]
    for id_, source, title, ft in specs:
        session.add(
            RawItem(
                id=id_,
                source=source,
                source_class="structured",
                url=f"https://x/{id_}",
                published_at=BASE,
                ingested_at=BASE,
                payload_json={
                    "title": title,
                    "description": "",
                    "extra": {"filing_type": ft} if ft else {},
                },
            )
        )
    session.flush()  # raw_items land before the clusters that FK to them
    for id_, *_ in specs:
        session.add(
            Cluster(
                cluster_id=id_,
                origin_item_id=id_,
                member_ids_json=[id_],
                origin_tier=0,
                member_count=1,
                created_at=BASE,
            )
        )
    session.commit()


def test_cluster_scored_once_and_idempotent(engine):
    with Session(engine) as s:
        _seed(s)
        n1 = score_clusters(s, finbert=FakeFinBERT(), lm=default_lm())
        assert n1 == 2
        assert s.execute(select(func.count()).select_from(ClusterScore)).scalar_one() == 2

        # Re-score: exactly one row per cluster, no duplicates (I5).
        score_clusters(s, finbert=FakeFinBERT(), lm=default_lm())
        assert s.execute(select(func.count()).select_from(ClusterScore)).scalar_one() == 2


def test_only_unscored_skips_scored_clusters(engine):
    # The fast-sweep path: after everything is scored, only_unscored is a no-op;
    # a full pass still re-scores everything.
    with Session(engine) as s:
        _seed(s)
        assert score_clusters(s, finbert=FakeFinBERT(), lm=default_lm(), only_unscored=True) == 2
        assert score_clusters(s, finbert=FakeFinBERT(), lm=default_lm(), only_unscored=True) == 0
        assert score_clusters(s, finbert=FakeFinBERT(), lm=default_lm()) == 2  # full re-score


def test_s1_neutral_tone_high_materiality(engine):
    with Session(engine) as s:
        _seed(s)
        score_clusters(s, finbert=FakeFinBERT(), lm=default_lm())
        s1 = s.get(ClusterScore, "s1")
        assert s1.catalyst_type == "ipo"
        assert s1.materiality >= 0.7 and s1.high_alert is True  # high materiality
        assert s1.predictive is False  # calendar-only
        assert abs(s1.lm_score) < 0.1  # tone ~neutral (the two-axis founding case)
