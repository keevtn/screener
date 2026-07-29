"""Signal engine (docs/ROADMAP.md task 4.2).

Turns a ticker's rolling-window composites into a contract-conformant prediction,
or abstains. Threshold rules on (sentiment composite, materiality, item count) ->
direction + confidence. Writes immutable ledger rows (I3 config_version, I4) whose
evidence is the contributing cluster ids. Structured-only for M1; social is
excluded (I8) and earnings clusters are deferred to the armed-drift path (4.5).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.aggregate.window import (
    ClusterContribution,
    WindowState,
    blended_sentiment,
    cluster_weight,
    compute_window,
)
from pipeline.common.models import Cluster, ClusterEntity, ClusterScore, Prediction, RawItem
from pipeline.common.schemas import PredictionIn
from pipeline.common.timeutil import utcnow


def _confidence(window: WindowState, params: dict[str, Any]) -> float:
    """Bounded [0.01, 1.0]: 0.5 at threshold, saturating to 1.0, nudged by materiality."""
    thr = params["sentiment_threshold"] or 1e-9
    over = max(0.0, (abs(window.sentiment_composite) - thr) / thr)
    base = 0.5 + 0.5 * min(1.0, over)
    mat_bonus = 0.1 * min(1.0, max(0.0, window.materiality_composite))
    return round(min(1.0, max(0.01, base + mat_bonus)), 4)


def evaluate_window(
    window: WindowState,
    params: dict[str, Any],
    *,
    config_version: str,
    now: datetime | None = None,
) -> PredictionIn | None:
    """Threshold rules -> a prediction, or None (abstain writes nothing)."""
    if window.item_count < params["min_items"]:
        return None
    s = window.sentiment_composite
    if abs(s) < params["sentiment_threshold"]:  # not directional enough -> abstain
        return None
    return PredictionIn(
        ticker=window.ticker,
        direction="bullish" if s > 0 else "bearish",
        confidence=_confidence(window, params),
        horizon_trading_days=params["horizon_trading_days"],
        threshold=params["threshold"],
        issued_at=now or utcnow(),
        config_version=config_version,
        evidence={"cluster_ids": window.contributing_cluster_ids},
    )


class SignalEngine:
    def __init__(
        self,
        session: Session,
        params: dict[str, Any],
        config_version: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self.session = session
        self.params = params
        self.config_version = config_version
        self.now = now or utcnow()

    def build_window(self, ticker: str) -> WindowState:
        # Structured-only (I8), predictive (ipo excluded), and NOT reaction_dependent
        # (earnings go through the armed-drift path in 4.5, not text direction).
        stmt = (
            select(ClusterScore, Cluster, RawItem)
            .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
            .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
            .join(RawItem, RawItem.id == Cluster.origin_item_id)
            .where(ClusterEntity.ticker == ticker)
            .where(RawItem.source_class != "social")
            .where(ClusterScore.predictive.is_(True))
            .where(ClusterScore.reaction_dependent.is_(False))
        )
        contribs: list[ClusterContribution] = []
        for cs, cluster, origin in self.session.execute(stmt).all():
            age_hours = (self.now - origin.published_at).total_seconds() / 3600.0
            sentiment = blended_sentiment(cs.finbert_score, cs.lm_score, self.params, cs.text_kind)
            weight = cluster_weight(
                cluster.origin_tier, age_hours, origin.source_class, self.params
            )
            contribs.append(
                ClusterContribution(cluster.cluster_id, sentiment, cs.materiality, weight)
            )
        return compute_window(ticker, contribs)

    def _recent_open_prediction(self, ticker: str) -> bool:
        """An open prediction for this ticker+config issued within the cooldown."""
        cutoff = self.now - timedelta(hours=float(self.params.get("cooldown_hours", 24.0)))
        stmt = (
            select(Prediction.prediction_id)
            .where(Prediction.ticker == ticker)
            .where(Prediction.config_version == self.config_version)
            .where(Prediction.status == "open")
            .where(Prediction.issued_at >= cutoff)
            .limit(1)
        )
        return self.session.execute(stmt).first() is not None

    def evaluate(self, ticker: str) -> Prediction | None:
        if self._recent_open_prediction(ticker):
            return None  # cooldown: don't re-emit for the same ticker every cycle
        window = self.build_window(ticker)
        pred_in = evaluate_window(
            window, self.params, config_version=self.config_version, now=self.now
        )
        if pred_in is None:
            return None
        pred = Prediction(
            ticker=pred_in.ticker,
            direction=pred_in.direction,
            confidence=pred_in.confidence,
            horizon_trading_days=pred_in.horizon_trading_days,
            threshold=pred_in.threshold,
            issued_at=pred_in.issued_at,
            config_version=pred_in.config_version,
            evidence_json=pred_in.evidence,
            status="open",
        )
        self.session.add(pred)
        self.session.commit()
        return pred

    def candidate_tickers(self) -> list[str]:
        """Tickers with at least one structured, predictive, non-reaction cluster."""
        stmt = (
            select(ClusterEntity.ticker)
            .join(Cluster, Cluster.cluster_id == ClusterEntity.cluster_id)
            .join(ClusterScore, ClusterScore.cluster_id == Cluster.cluster_id)
            .join(RawItem, RawItem.id == Cluster.origin_item_id)
            .where(RawItem.source_class != "social")
            .where(ClusterScore.predictive.is_(True))
            .where(ClusterScore.reaction_dependent.is_(False))
            .distinct()
        )
        return list(self.session.execute(stmt).scalars().all())

    def evaluate_all(self) -> list[Prediction]:
        preds = [self.evaluate(t) for t in self.candidate_tickers()]
        return [p for p in preds if p is not None]
