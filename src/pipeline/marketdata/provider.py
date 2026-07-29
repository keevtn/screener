"""Daily-bars provider with a local parquet cache (docs/ROADMAP.md task 0.4).

``MarketDataProvider.get_daily_bars(ticker, start, end)`` returns a canonical
DataFrame ``[date, open, high, low, adj_close, volume]``. Bars are cached to
``data/bars/<ticker>.parquet`` with a sidecar ``<ticker>.meta.json`` recording the
covered date span, so a repeat request inside a covered span performs zero HTTP.

ROADMAP-NOTE: the roadmap's `test_marketdata_cache` calls for a respx "zero HTTP"
assertion, but yfinance fetches over its own requests/curl_cffi stack, which respx
(an httpx mock) cannot intercept. The provider therefore takes an injectable
``downloader`` and the cache test counts downloader invocations instead — same
guarantee (no fetch on a cache hit), honestly testable without live network.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("pipeline.marketdata.provider")

import pandas as pd

BAR_COLUMNS = ["date", "open", "high", "low", "adj_close", "volume"]

# Requests reaching into today refetch at most this often (daily bars; the
# grader needs each session's close within one sweep-or-two of publication).
REFRESH_TTL = timedelta(hours=2)

# downloader(ticker, start, end) -> raw DataFrame (any schema; normalized below).
Downloader = Callable[[str, date, date], pd.DataFrame]


def _market_today() -> date:
    """The exchange's calendar day (US/Eastern) — the boundary of fetchable bars."""
    return datetime.now(tz=ZoneInfo("America/New_York")).date()


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _yfinance_download(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Default downloader. end is inclusive here; yfinance treats end as exclusive."""
    import yfinance as yf

    raw = yf.download(
        ticker,
        start=start.isoformat(),
        end=(pd.Timestamp(end) + pd.Timedelta(days=1)).date().isoformat(),
        auto_adjust=False,
        progress=False,
        actions=False,
        timeout=15,  # a dead connection must fail, not stall the sweep silently
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.rename(columns={c: str(c).strip().lower() for c in raw.columns})
    adj = "adj close" if "adj close" in raw.columns else "close"
    out = pd.DataFrame(
        {
            "date": raw.index,  # tz handled uniformly in _normalize
            "open": raw.get("open"),
            "high": raw.get("high"),
            "low": raw.get("low"),
            "adj_close": raw[adj],
            "volume": raw.get("volume"),
        }
    ).reset_index(drop=True)
    return out


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce to canonical BAR_COLUMNS, tz-naive normalized dates, sorted+deduped."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = frame.copy()
    dt = pd.to_datetime(df["date"])
    if getattr(dt.dt, "tz", None) is not None:
        dt = dt.dt.tz_convert(None)  # tz-aware (some yfinance builds) -> naive
    df["date"] = dt.dt.normalize()
    for col in BAR_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[BAR_COLUMNS].dropna(subset=["date", "adj_close"])
    df = df.drop_duplicates(subset="date", keep="last").sort_values("date")
    return df.reset_index(drop=True)


class MarketDataProvider:
    def __init__(
        self,
        cache_dir: str | Path = "data/bars",
        *,
        downloader: Downloader | None = None,
        benchmark: str = "SPY",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._download = downloader or _yfinance_download
        self.benchmark = benchmark

    # --- cache paths ---------------------------------------------------------
    def _parquet(self, ticker: str) -> Path:
        return self.cache_dir / f"{ticker.upper()}.parquet"

    def _meta(self, ticker: str) -> Path:
        return self.cache_dir / f"{ticker.upper()}.meta.json"

    def _load_cache(
        self, ticker: str
    ) -> tuple[pd.DataFrame, date | None, date | None, datetime | None]:
        pq, mp = self._parquet(ticker), self._meta(ticker)
        if not pq.exists() or not mp.exists():
            return pd.DataFrame(columns=BAR_COLUMNS), None, None, None
        df = _normalize(pd.read_parquet(pq))
        meta = json.loads(mp.read_text(encoding="utf-8"))
        cov_start = _as_date(meta["covered_start"]) if meta.get("covered_start") else None
        cov_end = _as_date(meta["covered_end"]) if meta.get("covered_end") else None
        fetched_at = (
            datetime.fromisoformat(meta["fetched_at"]) if meta.get("fetched_at") else None
        )
        return df, cov_start, cov_end, fetched_at

    def _write_cache(self, ticker: str, df: pd.DataFrame, cov_start: date, cov_end: date) -> None:
        _normalize(df).to_parquet(self._parquet(ticker), index=False)
        # NEVER claim coverage beyond today: recording the REQUESTED end poisoned
        # the cache permanently when callers asked 40 days ahead (marking's
        # window) — bars froze at whatever yfinance returned that day, starving
        # the grader forever. fetched_at powers the freshness rule below.
        meta = {
            "covered_start": cov_start.isoformat(),
            "covered_end": min(cov_end, _market_today()).isoformat(),
            "fetched_at": datetime.now(UTC).isoformat(),
        }
        self._meta(ticker).write_text(json.dumps(meta), encoding="utf-8")

    # --- public API ----------------------------------------------------------
    def get_daily_bars(
        self, ticker: str, start: date | datetime | str, end: date | datetime | str
    ) -> pd.DataFrame:
        """Bars for ``ticker`` over the inclusive [start, end] date range.

        Fetch policy: purely-historical requests (end before today) are served
        from coverage; any request reaching today (or beyond — callers may ask
        into the future) is refetched unless the last fetch is younger than
        REFRESH_TTL, so new session closes land without hammering yfinance.
        Returns a copy sliced to the requested range.
        """
        start_d, end_d = _as_date(start), _as_date(end)
        if start_d > end_d:
            raise ValueError(f"start {start_d} is after end {end_d}")

        cached, cov_start, cov_end, fetched_at = self._load_cache(ticker)
        today = _market_today()
        end_eff = min(end_d, today)  # the fetchable universe ends at today
        hist_covered = (
            cov_start is not None and cov_end is not None
            and cov_start <= start_d and end_eff <= cov_end
        )
        fresh_enough = end_eff < today or (
            fetched_at is not None
            and (datetime.now(UTC) - fetched_at) < REFRESH_TTL
        )
        if hist_covered and fresh_enough:
            df = cached
        else:
            # Fetch the union of the existing coverage and the new request in one shot.
            fetch_start = min(start_d, cov_start) if cov_start else start_d
            fetch_end = max(end_d, cov_end) if cov_end else end_d
            try:
                fetched = _normalize(self._download(ticker.upper(), fetch_start, fetch_end))
            except Exception:  # noqa: BLE001 — one retry, then degrade to cache
                try:
                    fetched = _normalize(self._download(ticker.upper(), fetch_start, fetch_end))
                except Exception as exc:  # noqa: BLE001
                    # Serve the (possibly stale) cached slice rather than crash the
                    # sweep; callers that need the missing dates skip honestly.
                    # LOG it — a silent stale-cache serve once let the grader run on
                    # frozen bars with zero visibility that the fetch had died.
                    log.warning(
                        "bar fetch failed twice for %s (%s) — serving cached slice "
                        "(may be stale through %s)",
                        ticker.upper(), type(exc).__name__, cov_end,
                    )
                    mask = (cached["date"] >= pd.Timestamp(start_d)) & (
                        cached["date"] <= pd.Timestamp(end_d)
                    )
                    return cached.loc[mask].reset_index(drop=True)
            # Skip empties: concatenating with the all-object empty cache frame
            # would upcast numeric columns to object.
            frames = [f for f in (cached, fetched) if not f.empty]
            df = _normalize(pd.concat(frames, ignore_index=True)) if frames else fetched
            self._write_cache(ticker, df, fetch_start, fetch_end)

        mask = (df["date"] >= pd.Timestamp(start_d)) & (df["date"] <= pd.Timestamp(end_d))
        return df.loc[mask].reset_index(drop=True)

    def cached_bars(self, ticker: str, *, days: int = 120) -> list[dict[str, Any]]:
        """Last ``days`` daily OHLC bars from the parquet cache ONLY (never fetches).

        Returns [{date, open, high, low, close, volume}] oldest->newest for the
        ticker chart's price panel (candlestick or line); empty if not cached.
        """
        df, _, _, _ = self._load_cache(ticker)
        if df.empty:
            return []
        def _px(row: Any, col: str, fallback: float) -> float:
            v = row.get(col)
            return round(float(v), 2) if pd.notna(v) else fallback

        tail = df.tail(days)
        out: list[dict[str, Any]] = []
        for _, row in tail.iterrows():
            close = round(float(row["adj_close"]), 2)
            out.append(
                {
                    "date": pd.Timestamp(row["date"]).date().isoformat(),
                    "open": _px(row, "open", close),
                    "high": _px(row, "high", close),
                    "low": _px(row, "low", close),
                    "close": close,
                    "volume": int(row["volume"]) if pd.notna(row["volume"]) else None,
                }
            )
        return out

    def cached_quote(self, ticker: str, *, vol_window: int = 20) -> dict[str, Any] | None:
        """Latest quote from the parquet cache ONLY — never fetches, so it is safe
        to call at request time (no yfinance rate-limit risk). Returns
        {last, pct_change, vol_over_avg, as_of} or None if the ticker is not
        cached or has fewer than two bars."""
        df, _, _, _ = self._load_cache(ticker)
        if df.empty or len(df) < 2:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        last_close, prev_close = float(last["adj_close"]), float(prev["adj_close"])
        pct = (last_close - prev_close) / prev_close * 100.0 if prev_close else None
        vols = df["volume"].dropna().tail(vol_window)
        avg_vol = float(vols.mean()) if len(vols) else None
        last_vol = float(last["volume"]) if pd.notna(last["volume"]) else None
        vol_over = (last_vol / avg_vol) if (avg_vol and last_vol) else None
        return {
            "ticker": ticker.upper(),
            "last": round(last_close, 2),
            "pct_change": round(pct, 2) if pct is not None else None,
            "vol_over_avg": round(vol_over, 2) if vol_over is not None else None,
            "as_of": pd.Timestamp(last["date"]).date().isoformat(),
        }

    def get_benchmark_bars(
        self, start: date | datetime | str, end: date | datetime | str
    ) -> pd.DataFrame:
        return self.get_daily_bars(self.benchmark, start, end)
