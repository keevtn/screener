"""Premarket Catalyst Ranker (PMR) — deterministic overnight-catalyst watchlist.

Every trading morning, rank the tickers whose catalysts fired between the previous
trading day's 16:00 ET close and now, merged with earnings *scheduled* for today,
so the open starts from a prioritized list instead of a raw tape. Panels freeze as
point-in-time artifacts (I12) at first compute >= 08:30 ET and grade themselves
after the close (gap + open->close vs the direction lean) — the evidence base a
future sim config would cite. Display/watchlist only: pipeline/sim must never read
PMR output; the sanctioned future trade path is a gated `min_premarket_rank` sim
filter term, not a wire from here.

Deterministic core: no LLM, no network; injected now/calendar everywhere; UTC-aware
throughout (I1). Buzz/scheduled-events inputs are optional (degrade gracefully).
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    PremarketPanel,
    RawItem,
    ScheduledEvent,
)
from pipeline.common.timeutil import ensure_utc
from pipeline.marketdata import CalendarRangeError, TradingCalendar

log = logging.getLogger("pipeline.premarket")

ET = ZoneInfo("America/New_York")

# Judgment call (c) in the PMR spec: overnight half-life is a panel constant, NOT a
# versioned-config key — this is a display ranking, not a signal (keeps I3 out of
# scope). exp(-age/H) e-folding, same convention as fired_panel's half_life_hours.
HALF_LIFE_OVERNIGHT_H = 6.0
PANEL_LIMIT = 25  # rows per panel; bounded so post-close grading is a bounded fetch

# ET boundaries of the premarket lifecycle.
PREV_CLOSE_ET = time(16, 0)  # window start anchor on the previous trading day
FREEZE_ET = time(8, 30)  # first compute at-or-after this freezes the day's snapshot
OPEN_ET = time(9, 30)  # snapshot must exist before the bell
GRADE_AFTER_ET = time(16, 30)  # same-day report cards only after the close settles

# Additive boosts on top of materiality x decay. Type weights: binary/structural
# events (FDA, halts, M&A, offerings) outrank routine flow.
TYPE_WEIGHTS: dict[str, float] = {
    "fda_action": 0.30,
    "halt": 0.30,
    "ma": 0.30,
    "secondary_offering": 0.30,
    "earnings_results": 0.20,
    "guidance_change": 0.20,
    "ipo": 0.15,
    "activist_stake": 0.15,
    "lockup_expiry": 0.15,
    "index_change": 0.15,
    "insider_cluster": 0.05,
    "product_pricing": 0.05,
}
HIGH_ALERT_BOOST = 0.25
SENTIMENT_BOOST = 0.20  # x max |finbert| across the ticker's overnight clusters
BUZZ_BOOST = 0.20  # x clamp(buzz_z, 0..5)/5 — social confirmation, never required
EARNINGS_TODAY_BOOST = 0.30

# Data honesty: scheduled_events does not persist a BMO/AMC session marker (the
# earnings feed parses the suffix for DATE math only, meta carries just
# {"approximate": true}). So the "BMO merge" degrades to "earnings scheduled
# TODAY" — flagged as such, never presented as a confirmed before-open call.


@dataclass(frozen=True)
class PremarketRow:
    """One ranked ticker on the morning panel (JSON-safe via asdict)."""

    ticker: str
    score: float
    lean: str  # long | short | mixed | none
    n_clusters: int
    scheduled_only: bool  # no overnight news — on the panel only for today's earnings
    earnings_today: bool  # earnings_results scheduled for this session (approximate)
    finbert_score: float | None  # signed, max-|.| across the ticker's clusters
    buzz_z: float | None
    # Top catalyst (the max-ranked cluster) — evidence surface for the row:
    cluster_id: str | None
    title: str | None
    catalyst_type: str | None
    event_stage: str | None
    materiality: float | None
    high_alert: bool
    published_at: str | None  # ISO, origin item
    age_hours: float | None  # at compute time


def premarket_window(calendar: TradingCalendar, now: datetime) -> tuple[datetime, datetime]:
    """[previous trading day 16:00 ET -> now] in UTC.

    Weekend/holiday-correct via the SPY-derived calendar (Fri close -> Mon morning
    spans the whole weekend). DST-correct via ZoneInfo — never a fixed offset.
    """
    now = ensure_utc(now)
    today_et = now.astimezone(ET).date()
    prev = calendar.prev_trading_day(today_et)
    start = datetime.combine(prev, PREV_CLOSE_ET, tzinfo=ET).astimezone(UTC)
    return start, now


def _et_moment(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


def _cluster_lean(catalyst_type: str | None, role: str | None, hint: str | None,
                  finbert: float | None) -> str | None:
    """Directional lean for one cluster: structural prior > classifier hint >
    sentiment sign. None when nothing clears its bar (never coin-flip)."""
    ct = (catalyst_type or "").lower()
    if ct in ("secondary_offering", "dilution", "atm_offering"):
        return "short"
    if ct in ("ma", "merger", "acquisition") and (role or "").lower() == "target":
        return "long"
    if hint == "bullish":
        return "long"
    if hint == "bearish":
        return "short"
    if finbert is not None and abs(finbert) >= 0.3:
        return "long" if finbert > 0 else "short"
    return None


def _resolve_lean(leans: list[str | None]) -> str:
    got = {x for x in leans if x}
    if not got:
        return "none"
    if len(got) == 1:
        return got.pop()
    return "mixed"


def premarket_panel(
    session: Session,
    calendar: TradingCalendar,
    now: datetime,
    *,
    buzz: dict[str, float] | None = None,
    limit: int = PANEL_LIMIT,
) -> list[PremarketRow]:
    """Rank tickers by overnight catalyst weight. Pure read; deterministic
    ordering (score desc, ticker asc). I12: only rows scored at-or-before now."""
    now = ensure_utc(now)
    start, end = premarket_window(calendar, now)
    today_et = now.astimezone(ET).date()

    rows = session.execute(
        select(ClusterScore, Cluster, RawItem, ClusterEntity.ticker, ClusterEntity.ticker_role)
        .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
        .where(RawItem.source_class == "structured")
        .where(RawItem.published_at >= start)
        .where(RawItem.published_at < end)
        .where(ClusterScore.created_at <= now)
    ).all()

    # Earnings scheduled for this session (approximate — see data-honesty note).
    earnings_today: set[str] = {
        tk
        for (tk,) in session.execute(
            select(ScheduledEvent.ticker)
            .where(ScheduledEvent.catalyst_type == "earnings_results")
            .where(ScheduledEvent.event_date == today_et)
            .where(ScheduledEvent.status == "upcoming")
        )
    }

    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for cs, cl, origin, ticker, role in rows:
        age_h = (now - ensure_utc(origin.published_at)).total_seconds() / 3600.0
        if age_h < 0:  # I12 belt-and-suspenders: future-dated items never rank
            continue
        crank = (cs.materiality or 0.0) * math.exp(-age_h / HALF_LIFE_OVERNIGHT_H)
        crank += TYPE_WEIGHTS.get((cs.catalyst_type or "").lower(), 0.0)
        if cs.high_alert:
            crank += HIGH_ALERT_BOOST
        by_ticker.setdefault(ticker, []).append(
            {
                "rank": crank,
                "cluster_id": cl.cluster_id,
                "title": (origin.payload_json or {}).get("title"),
                "catalyst_type": cs.catalyst_type,
                "event_stage": cs.event_stage,
                "materiality": cs.materiality,
                "high_alert": bool(cs.high_alert),
                "finbert": cs.finbert_score,
                "published_at": ensure_utc(origin.published_at).isoformat(),
                "age_h": age_h,
                "lean": _cluster_lean(cs.catalyst_type, role, cs.direction_hint, cs.finbert_score),
            }
        )

    out: list[PremarketRow] = []
    for ticker, clusters in by_ticker.items():
        top = max(clusters, key=lambda c: (c["rank"], c["cluster_id"]))
        fbs = [c["finbert"] for c in clusters if c["finbert"] is not None]
        fb = max(fbs, key=abs) if fbs else None
        z = (buzz or {}).get(ticker)
        score = top["rank"]
        if fb is not None:
            score += SENTIMENT_BOOST * abs(fb)
        if z is not None and z > 0:
            score += BUZZ_BOOST * min(z, 5.0) / 5.0
        if ticker in earnings_today:
            score += EARNINGS_TODAY_BOOST
        out.append(
            PremarketRow(
                ticker=ticker,
                score=round(score, 6),
                lean=_resolve_lean([c["lean"] for c in clusters]),
                n_clusters=len(clusters),
                scheduled_only=False,
                earnings_today=ticker in earnings_today,
                finbert_score=fb,
                buzz_z=z,
                cluster_id=top["cluster_id"],
                title=top["title"],
                catalyst_type=top["catalyst_type"],
                event_stage=top["event_stage"],
                materiality=top["materiality"],
                high_alert=top["high_alert"],
                published_at=top["published_at"],
                age_hours=round(top["age_h"], 3),
            )
        )

    # Earnings-today tickers with zero overnight news still make the panel,
    # clearly flagged as scheduled-only (they have a catalyst COMING, not fired).
    for ticker in sorted(earnings_today - set(by_ticker)):
        out.append(
            PremarketRow(
                ticker=ticker, score=round(EARNINGS_TODAY_BOOST, 6), lean="none",
                n_clusters=0, scheduled_only=True, earnings_today=True,
                finbert_score=None, buzz_z=(buzz or {}).get(ticker), cluster_id=None,
                title=None, catalyst_type="earnings_results", event_stage="scheduled",
                materiality=None, high_alert=False, published_at=None, age_hours=None,
            )
        )

    out.sort(key=lambda r: (-r.score, r.ticker))
    return out[:limit]


def persist_premarket_snapshot(
    session: Session,
    calendar: TradingCalendar,
    now: datetime,
    *,
    buzz: dict[str, float] | None = None,
) -> str:
    """Freeze today's panel exactly once, in the 08:30–09:30 ET window (spec
    judgment call (a): first compute at-or-after 08:30, not rolled to the bell).
    Cheap no-op every other sweep. Returns a _step-friendly status string."""
    now = ensure_utc(now)
    now_et = now.astimezone(ET)
    today = now_et.date()
    # A SPY-derived calendar cannot know TODAY is a trading day at 08:30 — today's
    # bar doesn't exist yet. Gate on weekday; a market holiday just produces an
    # inert panel that the grader closes out with an empty card (graded_n=0).
    if now_et.weekday() >= 5:
        return "skipped (weekend)"
    if not (FREEZE_ET <= now_et.time() < OPEN_ET):
        return "skipped (outside 08:30-09:30 ET)"
    if session.get(PremarketPanel, today) is not None:
        return "snapshot exists"

    start, end = premarket_window(calendar, now)
    rows = premarket_panel(session, calendar, now, buzz=buzz)
    session.add(
        PremarketPanel(
            session_date=today,
            computed_at=now,
            window_start=start,
            window_end=end,
            rows_json=[asdict(r) for r in rows],
            created_at=now,
        )
    )
    session.commit()
    log.info("premarket snapshot frozen: %s rows for %s", len(rows), today)
    return f"frozen {len(rows)} rows"


def _bars_for(provider: Any, ticker: str, start: date, end: date) -> dict[date, dict[str, float]]:
    """{date: {open, adj_close}} from the provider, empty on any failure."""
    try:
        df = provider.get_daily_bars(ticker, start, end)
    except Exception as exc:  # noqa: BLE001 — one bad ticker must not kill grading
        log.warning("premarket grade: bars failed for %s: %s", ticker, exc)
        return {}
    out: dict[date, dict[str, float]] = {}
    for _, row in df.iterrows():
        d = row["date"].date() if hasattr(row["date"], "date") else row["date"]
        out[d] = {"open": float(row["open"]), "adj_close": float(row["adj_close"])}
    return out


def _mean_abs_oc(outcomes: dict[str, dict[str, Any]], tks: list[str]) -> float | None:
    if not tks:
        return None
    return round(sum(abs(outcomes[t]["oc_return"]) for t in tks) / len(tks), 6)


def _trading_age(calendar: TradingCalendar, sd: date, today: date) -> int:
    """Trading days elapsed strictly after ``sd`` up to ``today``, capped at 2 —
    all the grading policy needs, and safe at the calendar's live edge (walking
    off the end just means "not old enough yet")."""
    n, cursor = 0, sd
    while n < 2:
        try:
            cursor = calendar.next_trading_day(cursor)
        except CalendarRangeError:
            break
        if cursor > today:
            break
        n += 1
    return n


def grade_premarket_panels(
    session: Session,
    provider: Any,
    calendar: TradingCalendar,
    now: datetime,
    *,
    max_panels: int = 5,
) -> str:
    """Post-close report cards (spec deliverable 9): stamp each ungraded panel's
    rows with realized gap (prev adj_close -> open) and open->close returns plus
    lean hit, from cached daily bars (I10 adjusted close; same-day adj_close ==
    close until later splits/dividends re-adjust — documented approximation).

    Grading is the ONLY post-freeze mutation (enforced by the ORM guard). A panel
    is stamped when every ticker has bars, or once it is >=2 trading days old
    (grade what exists — a delisted ticker must not block the card forever);
    otherwise it is left for the next sweep. Rank rows are never touched.
    """
    now = ensure_utc(now)
    now_et = now.astimezone(ET)
    today = now_et.date()
    pending = (
        session.execute(
            select(PremarketPanel)
            .where(PremarketPanel.graded_at.is_(None))
            .order_by(PremarketPanel.session_date.asc())
            .limit(max_panels)
        )
        .scalars()
        .all()
    )
    graded_panels = 0
    for panel in pending:
        sd = panel.session_date
        if sd > today or (sd == today and now_et.time() < GRADE_AFTER_ET):
            continue  # session not finished yet
        try:
            prev_td = calendar.prev_trading_day(sd)
        except CalendarRangeError:
            prev_td = sd - timedelta(days=1)  # degraded gap baseline off-calendar
        age_td = _trading_age(calendar, sd, today)
        tickers = [r["ticker"] for r in (panel.rows_json or [])]
        outcomes: dict[str, dict[str, Any]] = {}
        for r in panel.rows_json or []:
            tk = r["ticker"]
            bars = _bars_for(provider, tk, prev_td, sd)
            day, prev = bars.get(sd), bars.get(prev_td)
            if not day or not day.get("open"):
                continue
            oc = day["adj_close"] / day["open"] - 1.0
            gap = None
            if prev and prev.get("adj_close"):
                gap = day["open"] / prev["adj_close"] - 1.0
            lean = r.get("lean")
            hit = {"long": oc > 0, "short": oc < 0}.get(lean)
            outcomes[tk] = {
                "gap_return": round(gap, 6) if gap is not None else None,
                "oc_return": round(oc, 6),
                "lean_hit": hit,
            }
        if not outcomes:
            if age_td >= 2:
                # No session bars two trading days on — a market holiday (or an
                # all-delisted panel). Close it out with an empty card so it
                # doesn't sit pending forever.
                panel.outcomes_json = {}
                panel.summary_json = {
                    "graded_n": 0, "total": len(tickers), "top5_mean_abs_oc": None,
                    "rest_mean_abs_oc": None, "lean_hit_rate": None,
                }
                panel.graded_at = now
                graded_panels += 1
            continue  # else: no bars yet -> retry next sweep
        if len(outcomes) < len(tickers) and age_td < 2:
            continue  # partial coverage on a fresh panel -> retry for a fuller card
        top5 = [t for t in tickers[:5] if t in outcomes]
        rest = [t for t in tickers[5:] if t in outcomes]
        hits = [o["lean_hit"] for o in outcomes.values() if o["lean_hit"] is not None]
        panel.outcomes_json = outcomes
        panel.summary_json = {
            "graded_n": len(outcomes),
            "total": len(tickers),
            "top5_mean_abs_oc": _mean_abs_oc(outcomes, top5),
            "rest_mean_abs_oc": _mean_abs_oc(outcomes, rest),
            "lean_hit_rate": round(sum(hits) / len(hits), 4) if hits else None,
        }
        panel.graded_at = now
        graded_panels += 1
    session.commit()
    return f"graded {graded_panels} panel(s), {len(pending) - graded_panels} pending"
