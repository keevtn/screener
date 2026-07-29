"""Capture each closed sim trade's intraday MINUTE-BAR PATH into the bars cache.

The exit-bracket replay study (docs/sim_bracket_replay.md) needs the minute path
between a trade's entry and exit — but the live trading path prices from
``latest_trade`` (single quotes), so the minute-bar cache is never populated for
the tickers that actually trade. This module fills that gap two ways:

  * FORWARD (the clean way): the daily driver calls :func:`capture_trade_paths`
    at EOD (after the summary) for the day's closed trades — real bars accrue
    from that session on, out-of-sample.
  * BACKFILL (best-effort): a one-shot fetch of historical minute bars for past
    trades. IEX minute history is sparse on thin small-caps (the feed's known
    limit) — coverage is reported honestly, never fabricated.

Bars are merged into the SAME per-ticker parquet the replay reads
(data/bars_intraday/<T>.parquet), deduped on timestamp — compatible with
AlpacaData.cached_minute_bars' own writes.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger("pipeline.sim.capture")

_CACHE_DIR = Path("data/bars_intraday")
_COLS = ["time", "open", "high", "low", "close", "volume"]


def _merge_cache(cache_dir: Path, ticker: str, bars: list[dict[str, Any]]) -> int:
    """Merge freshly-fetched bars into the ticker's parquet (dedupe on time).
    Returns the total row count after merge. A corrupt existing file is replaced."""
    path = cache_dir / f"{ticker.upper().replace('/', '-')}.parquet"
    cached = pd.DataFrame()
    if path.exists():
        try:
            cached = pd.read_parquet(path)
        except Exception:  # noqa: BLE001 — corrupt cache is overwritten by fresh bars
            cached = pd.DataFrame()
    if not bars:
        return int(len(cached))
    merged = pd.concat([cached, pd.DataFrame(bars)], ignore_index=True)
    merged = merged.drop_duplicates(subset="time", keep="last").sort_values("time")
    cache_dir.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(path, index=False)
    return int(len(merged))


def capture_trade_paths(
    data: Any, trades: list[Any], *, cache_dir: Path | None = None, pad_min: int = 5
) -> dict[str, int]:
    """Fetch and cache the minute path covering each trade's holding window.

    ``data`` is anything with ``minute_bars(ticker, *, start, end)`` (AlpacaData
    in prod, a fake in tests). Returns {ticker: bars_fetched} — 0 means the feed
    returned nothing for that window (honest sparse coverage, not an error).
    """
    cache_dir = cache_dir or _CACHE_DIR
    by_ticker: dict[str, list[Any]] = defaultdict(list)
    for t in trades:
        if t.exited_at is not None:
            by_ticker[t.ticker].append(t)

    fetched: dict[str, int] = {}
    for ticker, ts in by_ticker.items():
        start = min(t.entered_at for t in ts) - timedelta(minutes=pad_min)
        end = max(t.exited_at for t in ts) + timedelta(minutes=pad_min)
        try:
            bars = data.minute_bars(ticker, start=start, end=end)
        except Exception as exc:  # noqa: BLE001 — one ticker's fetch must not abort the rest
            log.warning("capture: minute_bars failed for %s: %s", ticker, exc)
            fetched[ticker] = 0
            continue
        bars = [{k: b.get(k) for k in _COLS} for b in bars]
        _merge_cache(cache_dir, ticker, bars)
        fetched[ticker] = len(bars)
    return fetched
