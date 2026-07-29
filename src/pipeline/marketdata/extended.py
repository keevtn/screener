"""Extended-session tracker — per-day pre/regular/post price behavior, accumulated.

Logs one ``extended_session_daily`` row per tracked ticker per trading day so the
premarket and afterhours behavior of catalyst/sim names is recorded and days are
comparable over time (the user ask: "track the progress of them premarket and
postmarket; and track progress from previous days").

Data sources, honestly:
  - REGULAR session (prior_close / reg_open / reg_close / reg_pct): the daily bar
    cache (``MarketDataProvider``). Robust and fully backfillable — every history
    day is available.
  - EXTENDED session (pm_* / ah_*): intraday PREPOST bars (yfinance via
    ``intraday_bars``; the Alpaca minute cache for backfill). Best-effort: thin
    names simply have no pre/after-hours prints, which we store as NULL and the UI
    shows as "--" — never fabricated. yfinance keeps only ~recent days of 1m data,
    so extended history accrues FORWARD from when the logger starts; backfill is
    limited to whatever prepost bars the cache already holds.

Two phases (both ET-gated, idempotent upserts):
  - PREMARKET (~09:35 ET): prior_close, reg_open, pm_last/pct/high/low/volume.
  - POSTMARKET (after 20:00 ET, or next-day catch-up): reg_close/reg_pct from the
    daily cache, ah_last/pct/volume from the day's intraday prepost bars.
A phase only ever writes non-NULL values, so neither clobbers the other's fields.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import date as date_
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import ExtendedSessionDaily, PremarketPanel, SimTrade
from pipeline.common.timeutil import ensure_utc, utcnow

log = logging.getLogger("pipeline.extended")

ET = ZoneInfo("America/New_York")
_REG_OPEN_M = 9 * 60 + 30  # 09:30 ET in minutes-since-ET-midnight
_REG_CLOSE_M = 16 * 60  # 16:00 ET
SCOPE_CAP = 40  # max tickers tracked per day (bound the sequential intraday fetch)

# Injectable shapes (mirror the codebase's downloader-injection testing style).
IntradayFn = Callable[[str], dict[str, Any]]  # ticker -> {available, bars:[...]}
# ticker, start, end -> {date: {open, close}}
DailyBarsFn = Callable[[str, date_, date_], dict[date_, dict[str, float]]]


# --- pure summarizer ---------------------------------------------------------
def _norm_yf_bars(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """yfinance intraday cache rows -> normalized bars keyed by ET minute-of-day.

    The cache's ``time`` is an ET-shifted epoch (int(ET-wall-clock.timestamp())),
    so utcfromtimestamp recovers the ET wall clock; that gives us the session date
    and minute without any tz juggling."""
    out: list[dict[str, Any]] = []
    for b in records:
        t = b.get("time")
        if t is None or b.get("close") is None:
            continue
        # The cache's epoch is the ET wall-clock stamped as if UTC, so reading it
        # back in UTC recovers the ET hour/minute/date directly.
        wall = datetime.fromtimestamp(int(t), tz=UTC)
        out.append(
            {
                "date": wall.date(),
                "m": wall.hour * 60 + wall.minute,
                "o": b.get("open"),
                "h": b.get("high"),
                "l": b.get("low"),
                "c": b.get("close"),
                "v": b.get("volume"),
            }
        )
    return out


def summarize_extended(
    norm_bars: list[dict[str, Any]], prior_close: float | None
) -> dict[str, Any]:
    """Split normalized bars into pre/regular/after and compute the day's metrics.

    Pure: no I/O, no clock. ``norm_bars`` are {date, m(ET min), o,h,l,c,v}. Regular
    is 09:30<=m<16:00; premarket m<09:30; afterhours m>=16:00. All % vs the given
    prior_close / derived reg_close; None wherever a segment has no bars."""
    pm = [b for b in norm_bars if b["m"] < _REG_OPEN_M]
    reg = [b for b in norm_bars if _REG_OPEN_M <= b["m"] < _REG_CLOSE_M]
    ah = [b for b in norm_bars if b["m"] >= _REG_CLOSE_M]

    def _vol(bars: list[dict[str, Any]]) -> int | None:
        vs = [b["v"] for b in bars if b.get("v") is not None]
        return int(sum(vs)) if vs else None

    reg_open = reg[0]["o"] if reg else None
    reg_close = reg[-1]["c"] if reg else None
    pm_last = pm[-1]["c"] if pm else None
    pm_high = max((b["h"] for b in pm if b.get("h") is not None), default=None) if pm else None
    pm_low = min((b["l"] for b in pm if b.get("l") is not None), default=None) if pm else None
    ah_last = ah[-1]["c"] if ah else None

    def _pct(a: float | None, base: float | None) -> float | None:
        if a is None or not base:
            return None
        return round(a / base - 1.0, 6)

    return {
        "reg_open": reg_open,
        "reg_close": reg_close,
        "reg_pct": _pct(reg_close, prior_close),
        "pm_last": pm_last,
        "pm_pct": _pct(pm_last, prior_close),
        "pm_high": pm_high,
        "pm_low": pm_low,
        "pm_volume": _vol(pm),
        "ah_last": ah_last,
        "ah_pct": _pct(ah_last, reg_close),
        "ah_volume": _vol(ah),
    }


# --- persistence -------------------------------------------------------------
def _upsert(
    session: Session,
    ticker: str,
    d: date_,
    metrics: dict[str, Any],
    *,
    source: str,
    now: datetime,
) -> None:
    """Insert-or-update one (ticker, date) row, writing only NON-NULL metrics so a
    phase never wipes the other phase's fields."""
    row = session.get(ExtendedSessionDaily, (ticker, d))
    if row is None:
        row = ExtendedSessionDaily(ticker=ticker, session_date=d, created_at=now, updated_at=now)
        session.add(row)
    for k, v in metrics.items():
        if v is not None:
            setattr(row, k, v)
    row.source = source
    row.updated_at = now


def _prior_and_today(
    daily: dict[date_, dict[str, float]], today: date_
) -> tuple[float | None, dict[str, float] | None]:
    """(prior trading day's close, today's daily bar-or-None) from a {date:{open,
    close}} map. Prior close = the newest daily bar strictly before today."""
    prior_close = None
    for d in sorted(daily):
        if d < today and daily[d].get("close") is not None:
            prior_close = daily[d]["close"]
    return prior_close, daily.get(today)


def log_extended_session(
    session: Session,
    tickers: list[str],
    *,
    now: datetime,
    session_over: bool,
    intraday_fn: IntradayFn,
    daily_bars_fn: DailyBarsFn,
    source: str = "yfinance",
) -> str:
    """Log the day's extended-session row for each ticker (one phase).

    ``session_over`` gates the fields that are only final after the close: reg_close
    and reg_pct (unless taken from the daily cache, which is always final) and all
    ah_* (afterhours hasn't happened premarket). prior_close / reg_open / pm_* are
    written whenever available. Per-ticker failures are logged and skipped."""
    now = ensure_utc(now)
    today = now.astimezone(ET).date()
    win_start = today - timedelta(days=10)
    n = 0
    for t in tickers:
        try:
            daily = daily_bars_fn(t, win_start, today)
        except Exception as exc:  # noqa: BLE001 — one bad ticker must not kill the sweep
            log.warning("extended: daily bars failed for %s: %s", t, exc)
            daily = {}
        prior_close, today_daily = _prior_and_today(daily, today)

        try:
            intr = intraday_fn(t)
        except Exception as exc:  # noqa: BLE001
            log.warning("extended: intraday failed for %s: %s", t, exc)
            intr = {"available": False, "bars": []}
        norm = _norm_yf_bars(intr.get("bars", [])) if intr.get("available") else []
        # Only today's ET-dated intraday bars (yfinance 1d is today, but guard).
        norm = [b for b in norm if b["date"] == today]
        summ = summarize_extended(norm, prior_close)

        m: dict[str, Any] = {
            "prior_close": prior_close,
            # reg_open: daily cache open (final) if present, else the intraday open.
            "reg_open": (today_daily or {}).get("open") or summ["reg_open"],
            "pm_last": summ["pm_last"],
            "pm_pct": summ["pm_pct"],
            "pm_high": summ["pm_high"],
            "pm_low": summ["pm_low"],
            "pm_volume": summ["pm_volume"],
        }
        # reg_close: prefer the daily cache's official close (always final); else the
        # intraday last-regular close, but only once the session is over (premarket's
        # partial regular bars are NOT a close).
        reg_close = (today_daily or {}).get("close")
        if reg_close is None and session_over:
            reg_close = summ["reg_close"]
        if reg_close is not None:
            m["reg_close"] = reg_close
            m["reg_pct"] = round(reg_close / prior_close - 1.0, 6) if prior_close else None
        if session_over:
            m["ah_last"] = summ["ah_last"]
            m["ah_pct"] = (
                round(summ["ah_last"] / reg_close - 1.0, 6)
                if summ["ah_last"] is not None and reg_close
                else None
            )
            m["ah_volume"] = summ["ah_volume"]
        _upsert(session, t, today, m, source=source, now=now)
        n += 1
    session.commit()
    return f"logged {n} ticker(s) [{'post' if session_over else 'pre'}market]"


# --- ticker scope ------------------------------------------------------------
def active_extended_tickers(session: Session, now: datetime, *, cap: int = SCOPE_CAP) -> list[str]:
    """The day's tracked set: today's premarket-panel (overnight-catalyst) names +
    recently sim-traded names. Deterministic order, capped. This mirrors the PMR's
    'hot set' so the extended tracker follows the same catalyst names, plus whatever
    the sim actually touched."""
    now = ensure_utc(now)
    today = now.astimezone(ET).date()
    out: list[str] = []
    seen: set[str] = set()

    def _add(tk: str | None) -> None:
        if tk and tk not in seen:
            seen.add(tk)
            out.append(tk)

    panel = session.get(PremarketPanel, today)
    if panel is not None:
        for r in panel.rows_json or []:
            _add(r.get("ticker"))
    # sim names from the last few days (they carry positions across sessions).
    cutoff = now - timedelta(days=3)
    for (tk,) in session.execute(
        select(SimTrade.ticker).where(SimTrade.entered_at >= cutoff).distinct()
    ):
        _add(tk)
    return out[:cap]


# --- read side: history + movers + streaks ----------------------------------
def _row_dict(r: ExtendedSessionDaily) -> dict[str, Any]:
    return {
        "ticker": r.ticker,
        "date": r.session_date.isoformat(),
        "prior_close": r.prior_close,
        "pm_last": r.pm_last,
        "pm_pct": r.pm_pct,
        "pm_high": r.pm_high,
        "pm_low": r.pm_low,
        "pm_volume": r.pm_volume,
        "reg_open": r.reg_open,
        "reg_close": r.reg_close,
        "reg_pct": r.reg_pct,
        "ah_last": r.ah_last,
        "ah_pct": r.ah_pct,
        "ah_volume": r.ah_volume,
        "source": r.source,
    }


def premarket_streak(pm_pcts: list[float | None]) -> dict[str, Any]:
    """Consecutive same-sign premarket-move streak ending at the MOST RECENT entry.

    ``pm_pcts`` is oldest->newest. Returns {direction: 'gain'|'loss'|'flat'|None,
    count}. A None (no premarket prints that day) breaks the streak. Powers the
    'Nth straight premarket gain' context."""
    if not pm_pcts or pm_pcts[-1] is None:
        return {"direction": None, "count": 0}
    last = pm_pcts[-1]
    direction = "gain" if last > 0 else ("loss" if last < 0 else "flat")
    count = 0
    for v in reversed(pm_pcts):
        if v is None:
            break
        s = "gain" if v > 0 else ("loss" if v < 0 else "flat")
        if s != direction:
            break
        count += 1
    return {"direction": direction, "count": count}


def extended_history(session: Session, ticker: str, *, days: int = 30) -> dict[str, Any]:
    """A ticker's extended-session rows (newest first) + its current premarket streak."""
    ticker = ticker.strip().upper()
    rows = (
        session.execute(
            select(ExtendedSessionDaily)
            .where(ExtendedSessionDaily.ticker == ticker)
            .order_by(ExtendedSessionDaily.session_date.desc())
            .limit(days)
        )
        .scalars()
        .all()
    )
    asc = list(reversed(rows))
    streak = premarket_streak([r.pm_pct for r in asc])
    return {
        "ticker": ticker,
        "count": len(rows),
        "premarket_streak": streak,
        "rows": [_row_dict(r) for r in rows],
    }


def available_extended_dates(session: Session) -> list[date_]:
    """Distinct sessions that have at least one premarket mover (pm_pct set),
    newest first — the navigable set for the movers date selector. A date with only
    regular-session rows isn't listed (the movers view would be empty there)."""
    return list(
        session.execute(
            select(ExtendedSessionDaily.session_date)
            .where(ExtendedSessionDaily.pm_pct.is_not(None))
            .distinct()
            .order_by(ExtendedSessionDaily.session_date.desc())
        ).scalars()
    )


def date_label(d: date_) -> str:
    """'Fri Jul 24' — weekday + month + day, cross-platform (no %-d/%#d)."""
    return f"{d.strftime('%a %b')} {d.day}"


def extended_movers(session: Session, d: date_, *, limit: int = 50) -> dict[str, Any]:
    """Premarket movers for a day: rows with a premarket move, sorted by |pm_pct|
    desc, each carrying its trailing premarket streak (day-over-day context)."""
    rows = (
        session.execute(select(ExtendedSessionDaily).where(ExtendedSessionDaily.session_date == d))
        .scalars()
        .all()
    )
    movers = [r for r in rows if r.pm_pct is not None]
    movers.sort(key=lambda r: abs(r.pm_pct or 0.0), reverse=True)
    movers = movers[:limit]
    # trailing streaks: one history query per mover (bounded by `limit`).
    out = []
    for r in movers:
        hist = (
            session.execute(
                select(ExtendedSessionDaily.pm_pct)
                .where(ExtendedSessionDaily.ticker == r.ticker)
                .where(ExtendedSessionDaily.session_date <= d)
                .order_by(ExtendedSessionDaily.session_date.asc())
                .limit(30)
            )
            .scalars()
            .all()
        )
        item = _row_dict(r)
        item["premarket_streak"] = premarket_streak(list(hist))
        out.append(item)
    return {"date": d.isoformat(), "count": len(out), "movers": out}


# --- backfill from cached prepost parquet -----------------------------------
def backfill_from_cache(
    session: Session,
    *,
    daily_bars_fn: DailyBarsFn,
    cache_dir: str = "data/bars_intraday",
    now: datetime | None = None,
) -> str:
    """Best-effort historical backfill from the intraday parquet already on disk.

    yfinance ``<t>_1d.parquet`` (has the extended flag; one day each) and the Alpaca
    minute cache ``<t>.parquet`` (multi-day for sim names; ET-derived extended, thin
    afterhours) are parsed into per-(ticker, date) rows. Regular-session prices come
    from the daily cache. Only fills days genuinely present in the cache — honest
    about the thin history yfinance 1m retention allows."""
    import glob
    import os

    import pandas as pd

    now = ensure_utc(now or utcnow())
    made = 0
    by_key: dict[tuple[str, date_], list[dict[str, Any]]] = {}

    def _key(tk: str, d: date_) -> tuple[str, date_]:
        return (tk.upper(), d)

    for path in glob.glob(os.path.join(cache_dir, "*.parquet")):
        base = os.path.basename(path)
        if base.endswith("_1w.parquet"):
            continue
        try:
            df = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            continue
        if df.empty or "time" not in df.columns:
            continue
        if base.endswith("_1d.parquet"):
            tk = base[: -len("_1d.parquet")]
            for b in _norm_yf_bars(df.to_dict("records")):
                by_key.setdefault(_key(tk, b["date"]), []).append(b)
        else:
            tk = base[: -len(".parquet")]  # Alpaca minute cache: UTC ISO 'time'
            ts = pd.to_datetime(df["time"], utc=True).dt.tz_convert(ET)
            for i, w in enumerate(ts):
                c = df["close"].iloc[i]
                if pd.isna(c):
                    continue
                by_key.setdefault(_key(tk, w.date()), []).append(
                    {
                        "date": w.date(),
                        "m": w.hour * 60 + w.minute,
                        "o": df["open"].iloc[i],
                        "h": df["high"].iloc[i],
                        "l": df["low"].iloc[i],
                        "c": c,
                        "v": df["volume"].iloc[i] if "volume" in df.columns else None,
                    }
                )

    # daily-cache prior_close per ticker, fetched once across its span.
    for (tk, d), bars in sorted(by_key.items()):
        try:
            daily = daily_bars_fn(tk, d - timedelta(days=10), d)
        except Exception:  # noqa: BLE001
            daily = {}
        prior_close, today_daily = _prior_and_today(daily, d)
        summ = summarize_extended([b for b in bars if b["date"] == d], prior_close)
        m = {
            "prior_close": prior_close,
            "reg_open": (today_daily or {}).get("open") or summ["reg_open"],
            "reg_close": (today_daily or {}).get("close") or summ["reg_close"],
            "pm_last": summ["pm_last"],
            "pm_pct": summ["pm_pct"],
            "pm_high": summ["pm_high"],
            "pm_low": summ["pm_low"],
            "pm_volume": summ["pm_volume"],
            "ah_last": summ["ah_last"],
            "ah_pct": summ["ah_pct"],
            "ah_volume": summ["ah_volume"],
        }
        rc = m.get("reg_close")
        m["reg_pct"] = round(rc / prior_close - 1.0, 6) if rc and prior_close else None
        _upsert(session, tk, d, m, source="backfill", now=now)
        made += 1
    session.commit()
    return f"backfilled {made} (ticker,date) row(s) from {cache_dir}"
