"""Single-ticker "deep dive" analysis (docs/ROADMAP.md task 7.4).

An on-demand, deeper Claude read over ONE ticker — assembled entirely from our OWN
data (no internet): the ticker's recent attributed clusters (headlines + descriptions
+ both-axis scores), the rolling window composites the signal engine computes, its
attention/buzz series, any ledger predictions, the next scheduled earnings, and a
point-in-time fundamentals snapshot. One batched, cached-system model call returns a
structured analysis (thesis, direction lean, cited key evidence, risks, and what
would change the read).

Like the ranker this PROPOSES only (I6): it writes a `ticker_analyses` row and logs
spend, but never touches configs or the prediction ledger (I13). It is rate limited
server-side to N distinct tickers per rolling window and respects the daily soft cap.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.agents.client import (
    LLMClient,
    enforce_daily_cap,
    log_spend,
    resolve_model,
)
from pipeline.agents.schemas import DeepDiveOutput
from pipeline.aggregate.attention import buzz_z
from pipeline.common.models import (
    AttentionDaily,
    BuzzBaseline,
    Cluster,
    ClusterEntity,
    ClusterScore,
    FundamentalsSnapshot,
    Prediction,
    ScheduledEvent,
    TickerAnalysis,
)
from pipeline.common.timeutil import utcnow
from pipeline.signal.engine import SignalEngine

# Deep dive defaults to Sonnet 5 (selectable like the force-run), and is rate
# limited to DEEP_DIVE_MAX_TICKERS distinct tickers per DEEP_DIVE_WINDOW.
DEFAULT_DEEP_DIVE_MODEL = "claude-sonnet-5"
DEEP_DIVE_MAX_TICKERS = 2
DEEP_DIVE_WINDOW = timedelta(minutes=5)

_MAX_CLUSTERS = 12
_MAX_ATTENTION_DAYS = 21
_MAX_PREDICTIONS = 10
_MAX_UPCOMING = 6
_DESC_CHARS = 400

DEEP_DIVE_SYSTEM = """You are a disciplined equity analyst writing a focused single-ticker brief.

You receive a JSON "evidence" object for ONE ticker, assembled entirely from an
internal news-and-signal pipeline (no live internet): recent scored news "clusters"
(each with a cluster_id, headline, description, separate finbert_score and lm_score
in [-1, 1], catalyst_type, materiality in [0, 1], event_stage), a rolling "window"
of decayed sentiment/materiality composites, an "attention" series (news volume,
mean sentiment, buzz-z anomaly), any prior model "predictions" in the ledger, the
"next_earnings" date and other "upcoming_events", and a point-in-time
"fundamentals" snapshot. Some fields may be null when we have no data — say so
rather than inventing it.

Produce a structured deep dive:
- thesis: 2-4 sentences on what the evidence collectively implies for this ticker.
- direction: "bullish", "bearish", or "neutral" (your directional lean).
- conviction: a float in [0, 1] — your confidence, honest about thin evidence.
- key_evidence: the specific points that drive the thesis. Anchor each to a
  cluster_id from the evidence when it comes from a cluster (leave cluster_id null
  for window/attention/fundamentals-level points). Cite only cluster_ids present in
  the evidence.
- risks: what could break the thesis (conflicting signals, stale data, ambiguity).
- what_would_change_my_mind: concrete disconfirming signals you would watch for.

Rules:
- Use ONLY the provided evidence. Do not invent tickers, numbers, headlines, or
  events. If the evidence is thin, lower conviction and say so in the thesis.
- You PROPOSE an analysis; you never place trades or change any configuration.

Return ONLY a JSON object of the form:
{"thesis": "...", "direction": "...", "conviction": 0.0,
"key_evidence": [{"point": "...", "cluster_id": "..."}], "risks": ["..."],
"what_would_change_my_mind": ["..."]}
No prose outside the JSON."""

_RETRY_SUFFIX = (
    "\n\nYour previous reply was not valid JSON for the required schema. "
    "Reply again with ONLY the JSON object, nothing else."
)


class DeepDiveRateLimited(RuntimeError):
    """Raised when the per-window distinct-ticker rate limit is already reached.

    Carries ``retry_after`` (whole seconds) so the API can set a Retry-After header.
    """

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(
            f"deep-dive rate limit reached ({DEEP_DIVE_MAX_TICKERS} distinct tickers per "
            f"{int(DEEP_DIVE_WINDOW.total_seconds() // 60)} min); retry in {retry_after}s"
        )


def _round(x: float | None, n: int = 4) -> float | None:
    return round(x, n) if x is not None else None


def deep_dive_rate_status(
    session: Session, ticker: str, *, now: datetime | None = None
) -> tuple[bool, int]:
    """Rolling-window rate check: at most DEEP_DIVE_MAX_TICKERS DISTINCT tickers may
    be analyzed per DEEP_DIVE_WINDOW. Returns (allowed, retry_after_seconds).

    A re-run of a ticker already inside the window is always allowed (it adds no new
    distinct ticker), so the re-run button never trips the limit. Only real model
    calls count (status != 'empty'); empty no-data runs consume nothing.
    """
    now = now or utcnow()
    ticker = ticker.strip().upper()
    window_start = now - DEEP_DIVE_WINDOW
    rows = session.execute(
        select(TickerAnalysis.ticker, func.max(TickerAnalysis.created_at))
        .where(TickerAnalysis.created_at >= window_start)
        .where(TickerAnalysis.status != "empty")
        .group_by(TickerAnalysis.ticker)
    ).all()
    active = {t: last for t, last in rows}
    if ticker in active:
        return True, 0
    if len(active) < DEEP_DIVE_MAX_TICKERS:
        return True, 0
    # Full: the first slot frees when the ticker whose latest run is oldest ages out.
    soonest_free = min(active.values())
    retry_after = max(1, math.ceil((soonest_free + DEEP_DIVE_WINDOW - now).total_seconds()))
    return False, retry_after


def _recent_clusters(
    session: Session, ticker: str, now: datetime, *, limit: int = _MAX_CLUSTERS
) -> list[dict[str, Any]]:
    from pipeline.common.models import RawItem

    rows = session.execute(
        select(ClusterScore, Cluster, ClusterEntity, RawItem)
        .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
        .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .where(ClusterEntity.ticker == ticker)
        .order_by(RawItem.published_at.desc(), Cluster.cluster_id)
        .limit(limit)
    ).all()
    out: list[dict[str, Any]] = []
    for cs, cluster, ent, origin in rows:
        payload = origin.payload_json or {}
        desc = (payload.get("description") or "").strip()
        out.append(
            {
                "cluster_id": cluster.cluster_id,
                "published_at": origin.published_at.isoformat(),
                "source": origin.source,
                "source_class": origin.source_class,
                "tier": cluster.origin_tier,
                "ticker_role": ent.ticker_role,
                "title": payload.get("title"),
                "description": desc[:_DESC_CHARS] if desc else None,
                "text_kind": cs.text_kind,
                "finbert_score": _round(cs.finbert_score),
                "lm_score": _round(cs.lm_score),
                "catalyst_type": cs.catalyst_type,
                "event_stage": cs.event_stage,
                "materiality": _round(cs.materiality),
                "direction_hint": cs.direction_hint,
                "high_alert": cs.high_alert,
            }
        )
    return out


def _attention_series(
    session: Session, ticker: str, now: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    cutoff = now.date() - timedelta(days=_MAX_ATTENTION_DAYS)
    baseline = session.get(BuzzBaseline, ticker)
    rows = (
        session.execute(
            select(AttentionDaily)
            .where(AttentionDaily.ticker == ticker, AttentionDaily.date >= cutoff)
            .order_by(AttentionDaily.date)
        )
        .scalars()
        .all()
    )
    series = [
        {
            "date": a.date.isoformat(),
            "struct": a.struct_count,
            "social": a.social_count,
            "sentiment": _round(a.sentiment_mean, 3),
            "buzz_z": buzz_z(a.social_count, baseline),
        }
        for a in rows
    ]
    base = (
        {"mean": baseline.mean, "std": baseline.std, "n_days": baseline.n_days,
         "source": baseline.source}
        if baseline
        else None
    )
    return series, base


def _predictions(session: Session, ticker: str) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            select(Prediction)
            .where(Prediction.ticker == ticker)
            .order_by(Prediction.issued_at.desc())
            .limit(_MAX_PREDICTIONS)
        )
        .scalars()
        .all()
    )
    return [
        {
            "prediction_id": p.prediction_id,
            "direction": p.direction,
            "confidence": _round(p.confidence),
            "horizon_trading_days": p.horizon_trading_days,
            "issued_at": p.issued_at.isoformat(),
            "status": p.status,
            "outcome": p.outcome,
            "realized_adjusted_return": _round(p.realized_adjusted_return),
            "config_version": p.config_version,
        }
        for p in rows
    ]


def _events(session: Session, ticker: str, now: datetime) -> tuple[str | None, list[dict[str, Any]]]:
    today = now.date()
    upcoming = (
        session.execute(
            select(ScheduledEvent)
            .where(
                ScheduledEvent.ticker == ticker,
                ScheduledEvent.status == "upcoming",
                ScheduledEvent.event_date >= today,
            )
            .order_by(ScheduledEvent.event_date)
            .limit(_MAX_UPCOMING)
        )
        .scalars()
        .all()
    )
    next_earnings = next(
        (e.event_date.isoformat() for e in upcoming if e.catalyst_type == "earnings_results"),
        None,
    )
    events = [
        {
            "catalyst_type": e.catalyst_type,
            "event_date": e.event_date.isoformat(),
            "stage": e.stage,
            "source": e.source,
        }
        for e in upcoming
    ]
    return next_earnings, events


def _fundamentals(session: Session, ticker: str, now: datetime) -> dict[str, Any] | None:
    """Nearest fundamentals snapshot at-or-before today (point-in-time, I12)."""
    row = session.execute(
        select(FundamentalsSnapshot)
        .where(
            FundamentalsSnapshot.ticker == ticker,
            FundamentalsSnapshot.as_of <= now.date(),
        )
        .order_by(FundamentalsSnapshot.as_of.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "as_of": row.as_of.isoformat(),
        "provider": row.provider,
        "sector": row.sector,
        "industry": row.industry,
        "market_cap": row.market_cap,
        "price": row.price,
        "change_pct": row.change_pct,
        "avg_volume": row.avg_volume,
        "short_float": row.short_float,
        "inst_own": row.inst_own,
        "insider_own": row.insider_own,
        "beta": row.beta,
    }


def assemble_evidence(
    session: Session,
    ticker: str,
    params: dict[str, Any],
    config_version: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Own-data evidence bundle for one ticker (no internet). JSON-serializable.

    ``is_empty`` is True when the ticker has no clusters, no window items, and no
    attention history — nothing worth spending a model call on.
    """
    now = now or utcnow()
    ticker = ticker.strip().upper()
    window = SignalEngine(session, params, config_version, now=now).build_window(ticker)
    clusters = _recent_clusters(session, ticker, now)
    attention, baseline = _attention_series(session, ticker, now)
    predictions = _predictions(session, ticker)
    next_earnings, upcoming = _events(session, ticker, now)
    fundamentals = _fundamentals(session, ticker, now)

    is_empty = not clusters and window.item_count == 0 and not attention
    return {
        "ticker": ticker,
        "as_of": now.isoformat(),
        "config_version": config_version,
        "is_empty": is_empty,
        "window": {
            "sentiment_composite": _round(window.sentiment_composite),
            "materiality_composite": _round(window.materiality_composite),
            "item_count": window.item_count,
            "total_weight": _round(window.total_weight),
        },
        "clusters": clusters,
        "attention": attention,
        "buzz_baseline": baseline,
        "predictions": predictions,
        "next_earnings": next_earnings,
        "upcoming_events": upcoming,
        "fundamentals": fundamentals,
    }


def _extract_json(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def parse_deep_dive_output(text: str) -> DeepDiveOutput:
    """Parse + validate the model reply; raises ValueError/ValidationError on bad output."""
    return DeepDiveOutput.model_validate(json.loads(_extract_json(text)))


def _persist(
    session: Session,
    *,
    ticker: str,
    model: str,
    config_version: str,
    horizon: int,
    now: datetime,
    status: str,
    evidence: dict[str, Any],
    parsed: DeepDiveOutput | None = None,
    error: str | None = None,
) -> TickerAnalysis:
    key_evidence: list[dict[str, Any]] = []
    risks: list[str] = []
    wwc: list[str] = []
    direction = conviction = thesis = None
    if parsed is not None:
        valid_ids = {c["cluster_id"] for c in evidence.get("clusters", [])}
        for pt in parsed.key_evidence:
            cid = pt.cluster_id if pt.cluster_id in valid_ids else None
            key_evidence.append({"point": pt.point, "cluster_id": cid})
        risks = list(parsed.risks)
        wwc = list(parsed.what_would_change_my_mind)
        direction = parsed.direction
        conviction = parsed.conviction
        thesis = parsed.thesis
    analysis = TickerAnalysis(
        ticker=ticker,
        created_at=now,
        model=model,
        horizon_trading_days=horizon,
        config_version=config_version,
        status=status,
        direction=direction,
        conviction=conviction,
        thesis=thesis,
        key_evidence_json=key_evidence,
        risks_json=risks,
        what_would_change_json=wwc,
        evidence_json=evidence,
        error=error,
    )
    session.add(analysis)
    session.commit()
    return analysis


def run_deep_dive(
    session: Session,
    client: LLMClient,
    ticker: str,
    *,
    params: dict[str, Any],
    config_version: str,
    model: str = DEFAULT_DEEP_DIVE_MODEL,
    horizon_trading_days: int | None = None,
    now: datetime | None = None,
    cap: float | None = None,
    max_tokens: int = 2048,
    enforce_rate_limit: bool = True,
) -> TickerAnalysis:
    """Assemble the ticker's own-data evidence, run one Claude call, persist the
    structured analysis. Rate limited + daily-cap guarded; spend logged per call.
    """
    now = now or utcnow()
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker must be non-empty")
    model = resolve_model(model)
    horizon = int(horizon_trading_days or params["horizon_trading_days"])

    evidence = assemble_evidence(session, ticker, params, config_version, now=now)
    if evidence["is_empty"]:
        # Nothing to analyze — honest empty, no model call, no rate/cap consumption.
        return _persist(
            session, ticker=ticker, model=model, config_version=config_version,
            horizon=horizon, now=now, status="empty", evidence=evidence,
            error="no own-data evidence for this ticker yet",
        )

    if enforce_rate_limit:
        allowed, retry_after = deep_dive_rate_status(session, ticker, now=now)
        if not allowed:
            raise DeepDiveRateLimited(retry_after)
    enforce_daily_cap(session, cap=cap, now=now)  # refuse if today's spend is at the cap

    user = (
        f"Ticker: {ticker}\nTrading-day horizon: {horizon}\n"
        f"Evidence (JSON):\n{json.dumps(evidence, ensure_ascii=False)}"
    )
    parsed: DeepDiveOutput | None = None
    for attempt in range(2):  # one retry on invalid JSON (mirrors the ranker)
        prompt = user if attempt == 0 else user + _RETRY_SUFFIX
        try:
            result = client.complete(
                system=DEEP_DIVE_SYSTEM, user=prompt, model=model, max_tokens=max_tokens
            )
        except Exception as exc:  # noqa: BLE001 — provider/transport failure: persist
            # the reason (mirrors the ranker) instead of 500ing without CORS headers.
            return _persist(
                session, ticker=ticker, model=model, config_version=config_version,
                horizon=horizon, now=now, status="failed", evidence=evidence,
                error=f"llm call failed: {exc}"[:500],
            )
        try:
            parsed = parse_deep_dive_output(result.text)
            log_spend(session, result, purpose="deep_dive", ok=True, now=now)
            break
        except (ValueError, ValidationError):
            log_spend(session, result, purpose="deep_dive", ok=False, now=now)

    if parsed is None:
        return _persist(
            session, ticker=ticker, model=model, config_version=config_version,
            horizon=horizon, now=now, status="failed", evidence=evidence,
            error="deep-dive output failed schema validation after one retry",
        )
    return _persist(
        session, ticker=ticker, model=model, config_version=config_version,
        horizon=horizon, now=now, status="ok", evidence=evidence, parsed=parsed,
    )


def latest_analysis(session: Session, ticker: str) -> TickerAnalysis | None:
    """Most recent persisted analysis for a ticker (instant revisit)."""
    return session.execute(
        select(TickerAnalysis)
        .where(TickerAnalysis.ticker == ticker.strip().upper())
        .order_by(TickerAnalysis.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
