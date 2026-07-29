"""Catalyst-armed drift mode, PEAD (docs/ROADMAP.md task 4.5).

Earnings (reaction_dependent) clusters arm a ticker instead of contributing text
direction. The first post-event session's market-adjusted reaction sets the drift
direction; |reaction| ≥ config threshold emits a continuation prediction in that
direction (reaction beats text). Unresolved armed states expire after a config TTL.
Reuses the grader's calendar + clock-start (I12: reaction close is strictly after t0).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import (
    ArmedState,
    Cluster,
    ClusterEntity,
    ClusterScore,
    Prediction,
    RawItem,
)
from pipeline.common.timeutil import utcnow
from pipeline.grade.grader import (
    DEFAULT_CLOSE_TIME,
    DEFAULT_EXCHANGE_TZ,
    Grader,
    _closes_by_date,
)
from pipeline.marketdata import CalendarRangeError, TradingCalendar


def arm_ticker(
    session: Session,
    ticker: str,
    cluster_id: str,
    catalyst_type: str,
    event_ts: datetime,
    *,
    now: datetime | None = None,
) -> ArmedState:
    """Idempotently arm a (ticker, cluster) pair."""
    now = now or utcnow()
    existing = session.execute(
        select(ArmedState).where(ArmedState.ticker == ticker, ArmedState.cluster_id == cluster_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    state = ArmedState(
        ticker=ticker,
        cluster_id=cluster_id,
        catalyst_type=catalyst_type,
        event_ts=event_ts,
        armed_at=now,
        status="armed",
        created_at=now,
    )
    session.add(state)
    session.commit()
    return state


def arm_reaction_dependent(session: Session, *, now: datetime | None = None) -> int:
    """Arm every (ticker, cluster) for reaction_dependent, structured clusters."""
    now = now or utcnow()
    stmt = (
        select(
            ClusterEntity.ticker,
            Cluster.cluster_id,
            ClusterScore.catalyst_type,
            RawItem.published_at,
        )
        .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
        .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .where(ClusterScore.reaction_dependent.is_(True))
        .where(RawItem.source_class != "social")
    )
    n = 0
    for ticker, cluster_id, catalyst_type, event_ts in session.execute(stmt).all():
        arm_ticker(
            session, ticker, cluster_id, catalyst_type or "earnings_results", event_ts, now=now
        )
        n += 1
    return n


def market_adjusted_reaction(
    provider: Any,
    ticker: str,
    event_ts: datetime,
    *,
    exchange_tz: str = DEFAULT_EXCHANGE_TZ,
    close_time: str = DEFAULT_CLOSE_TIME,
) -> float | None:
    """First-post-event-close market-adjusted reaction, or None if no bar yet (I12)."""
    win_start = event_ts.date() - timedelta(days=10)
    win_end = event_ts.date() + timedelta(days=21)
    spy = provider.get_benchmark_bars(win_start, win_end)
    if spy.empty:
        return None
    calendar = TradingCalendar.from_bars(spy)
    try:
        c0 = Grader(provider, exchange_tz=exchange_tz, close_time=close_time).clock_start_date(
            event_ts, calendar
        )
    except CalendarRangeError:
        return None  # no post-event session yet

    spy_close = _closes_by_date(spy)
    dates = sorted(spy_close)
    if c0 not in spy_close:
        return None
    idx = dates.index(c0)
    if idx == 0:  # need a pre-event close for the baseline
        return None
    prev = dates[idx - 1]

    tk_close = _closes_by_date(provider.get_daily_bars(ticker, win_start, win_end))
    if c0 not in tk_close or prev not in tk_close:
        return None
    return (tk_close[c0] / tk_close[prev] - 1.0) - (spy_close[c0] / spy_close[prev] - 1.0)


def resolve_armed_state(
    session: Session,
    armed: ArmedState,
    provider: Any,
    params: dict[str, Any],
    config_version: str,
    *,
    now: datetime | None = None,
) -> Prediction | None:
    """Resolve one armed state: emit a continuation, expire, or stay armed."""
    now = now or utcnow()
    if armed.status != "armed":
        return None
    cfg = params.get("armed", {})
    reaction = market_adjusted_reaction(
        provider,
        armed.ticker,
        armed.event_ts,
        exchange_tz=params.get("exchange_tz", DEFAULT_EXCHANGE_TZ),
        close_time=params.get("close_time", DEFAULT_CLOSE_TIME),
    )

    if reaction is None:
        # No post-event close yet: expire past TTL, else remain armed.
        if (now - armed.armed_at).total_seconds() / 3600.0 > float(cfg.get("ttl_hours", 96.0)):
            armed.status = "expired"
            armed.resolution = "ttl_no_bars"
            session.commit()
        return None

    if abs(reaction) < float(cfg.get("reaction_threshold", 0.02)):
        armed.status = "resolved"
        armed.resolution = "no_signal"
        session.commit()
        return None

    pred = Prediction(
        ticker=armed.ticker,
        direction="bullish" if reaction > 0 else "bearish",  # reaction beats text
        confidence=round(min(1.0, 0.5 + abs(reaction) * 5.0), 4),
        horizon_trading_days=params["horizon_trading_days"],
        threshold=params["threshold"],
        issued_at=now,
        config_version=config_version,
        evidence_json={
            "armed_cluster_id": armed.cluster_id,
            "catalyst_type": armed.catalyst_type,
            "reaction": round(reaction, 6),
        },
        status="open",
    )
    session.add(pred)
    armed.status = "resolved"
    armed.resolution = "emitted"
    session.commit()
    return pred


def resolve_all_armed(
    session: Session,
    provider: Any,
    params: dict[str, Any],
    config_version: str,
    *,
    now: datetime | None = None,
) -> list[Prediction]:
    armed = session.execute(select(ArmedState).where(ArmedState.status == "armed")).scalars().all()
    out = [
        resolve_armed_state(session, a, provider, params, config_version, now=now) for a in armed
    ]
    return [p for p in out if p is not None]
