"""Signal-lab marking job (docs/ROADMAP.md task 5c.2).

Entry price = first close strictly after t0 (I12 — after-hours events never use the
just-finished session). Marks = cumulative abnormal return (ticker − SPY) at
+1/+2/+3/+5/+10 trading days from entry, plus a volatility-scaled variant
(CAR ÷ trailing daily vol). An observation matures once its +10d close exists; open
observations expose a running CAR. Idempotent: matured rows are never re-marked.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.models import SignalObservation
from pipeline.common.timeutil import utcnow
from pipeline.grade.grader import DEFAULT_CLOSE_TIME, DEFAULT_EXCHANGE_TZ, Grader, _closes_by_date
from pipeline.marketdata import CalendarRangeError, TradingCalendar

CAR_HORIZONS = (1, 2, 3, 5, 10)
_MATURE_HORIZON = 10
_VOL_LOOKBACK = 20
# Confounding control (5c.3): another material cluster on the same ticker within
# ±this many trading days contaminates the clean window.
_CLEAN_WINDOW_TDAYS = 3
_MATERIAL_FLOOR = 0.5  # ROADMAP-NOTE: config-tunable later


def _td_distance(trading_days: list, d1, d2) -> int:
    """Number of trading sessions between two dates (calendar-derived)."""
    lo, hi = (d1, d2) if d1 <= d2 else (d2, d1)
    return sum(1 for d in trading_days if lo < d <= hi)


def _finalize_clean_window(session: Session, obs: SignalObservation, trading_days: list) -> None:
    """Clean iff no other MATERIAL cluster on the same ticker within ±3 trading days.

    Finalized at maturity because future events contaminate too (5c.3).
    """
    others = (
        session.execute(
            select(SignalObservation)
            .where(SignalObservation.ticker == obs.ticker)
            .where(SignalObservation.observation_id != obs.observation_id)
        )
        .scalars()
        .all()
    )
    for o in others:
        materiality = (o.features_json or {}).get("materiality") or 0.0
        if materiality < _MATERIAL_FLOOR:
            continue
        if _td_distance(trading_days, obs.t0.date(), o.t0.date()) <= _CLEAN_WINDOW_TDAYS:
            obs.clean_window = False
            return
    obs.clean_window = True


# A single-session close ratio beyond this is a split/adjustment break in the
# bar series, not a market move (observed: XAIR 19x day jump -> +1,783% "CAR").
_SUSPECT_DAY_RATIO = 4.0


def series_suspect(tk_close: dict, entry) -> bool:
    """True when the from-entry bar series contains a single-day 4x jump/collapse."""
    days = sorted(d for d in tk_close if d >= entry)
    for i in range(1, len(days)):
        prev, cur = tk_close[days[i - 1]], tk_close[days[i]]
        if prev and (cur / prev > _SUSPECT_DAY_RATIO or cur / prev < 1.0 / _SUSPECT_DAY_RATIO):
            return True
    return False


def _trailing_daily_vol(tk_close: dict, entry) -> float | None:
    prior = sorted(d for d in tk_close if d < entry)[-(_VOL_LOOKBACK + 1) :]
    if len(prior) < 3:
        return None
    rets = [tk_close[prior[i]] / tk_close[prior[i - 1]] - 1.0 for i in range(1, len(prior))]
    vol = float(np.std(rets))
    return vol or None


def mark_observation(
    session: Session,
    obs: SignalObservation,
    provider: Any,
    *,
    exchange_tz: str = DEFAULT_EXCHANGE_TZ,
    close_time: str = DEFAULT_CLOSE_TIME,
) -> bool:
    """Compute entry + CAR marks for one open observation. Returns True if matured."""
    win_start = obs.t0.date() - timedelta(days=_VOL_LOOKBACK * 3 + 15)
    win_end = obs.t0.date() + timedelta(days=40)
    spy = provider.get_benchmark_bars(win_start, win_end)
    if spy.empty:
        return False
    calendar = TradingCalendar.from_bars(spy)
    try:
        entry = Grader(provider, exchange_tz=exchange_tz, close_time=close_time).clock_start_date(
            obs.t0, calendar
        )
    except CalendarRangeError:
        return False

    spy_close = _closes_by_date(spy)
    tk_close = _closes_by_date(provider.get_daily_bars(obs.ticker, win_start, win_end))
    if entry not in spy_close or entry not in tk_close:
        return False

    horizons = sorted(d for d in spy_close if d > entry)[:_MATURE_HORIZON]
    base_t, base_m = tk_close[entry], spy_close[entry]
    marks: dict[str, Any] = {}
    for k in CAR_HORIZONS:
        if len(horizons) < k:
            continue
        d = horizons[k - 1]
        if d not in tk_close or d not in spy_close:
            continue
        car = (tk_close[d] / base_t - 1.0) - (spy_close[d] / base_m - 1.0)
        marks[f"car_{k}d"] = round(car, 6)

    vol = _trailing_daily_vol(tk_close, entry)
    if vol:
        for k in CAR_HORIZONS:
            if f"car_{k}d" in marks:
                marks[f"car_{k}d_volscaled"] = round(marks[f"car_{k}d"] / vol, 4)
        marks["trailing_vol"] = round(vol, 6)

    if series_suspect(tk_close, entry):
        marks["suspect_series"] = True  # artifact guard — analysis excludes these

    obs.entry_price_date = entry
    obs.marks_json = marks
    matured = (
        len(horizons) >= _MATURE_HORIZON
        and horizons[_MATURE_HORIZON - 1] in tk_close
        and horizons[_MATURE_HORIZON - 1] in spy_close
    )
    if matured:
        obs.status = "matured"
        _finalize_clean_window(session, obs, sorted(spy_close))
    session.commit()
    return matured


def mark_observations(
    session: Session,
    provider: Any,
    *,
    now: datetime | None = None,
    max_marks: int | None = None,
) -> tuple[int, int]:
    """Mark open observations (oldest t0 first). Returns (marked, matured).

    ``max_marks`` bounds the number processed PER CALL so a large post-downtime
    backlog can't monopolize a pipeline sweep and starve the fast ingest->score
    path behind it. The bounded slice is RANDOM, not oldest-first: a large subset
    of old observations can be permanently un-maturable (thin/gap tickers with no
    usable bars at their entry date), and any fixed ordering would re-process that
    same dead front every sweep and starve the rest. Random sampling guarantees
    every open observation is processed over successive sweeps and still bounds
    the per-sweep cost. Marking is idempotent, so slicing across sweeps is safe.
    None = unbounded (tests / manual full passes)."""
    _ = now or utcnow()
    stmt = select(SignalObservation).where(SignalObservation.status == "open")
    if max_marks is not None:
        stmt = stmt.order_by(func.random()).limit(max_marks)
    open_obs = session.execute(stmt).scalars().all()
    marked = matured = 0
    for obs in open_obs:
        did_mature = mark_observation(session, obs, provider)
        if obs.marks_json:
            marked += 1
        if did_mature:
            matured += 1
    return marked, matured
