"""Volatility helpers shared by the sim driver (vol_stop exits) and the API
(advisory overlay). Pure functions over daily OHLC bars — no I/O, no coupling to
sim or api, so both layers can import them without a cycle."""

from __future__ import annotations

from typing import Any


def atr_fraction(daily_bars: list[dict[str, Any]], period: int = 14) -> float | None:
    """Average True Range as a fraction of the last close, from DAILY OHLC bars
    (oldest->newest, each with high/low/close). None when there isn't enough
    history or the last close is non-positive.

    This is the 'recent vol' input the vol_stop exit is scaled by: the adverse
    stop sits at ``atr_mult × atr_fraction`` below (long) / above (short) entry.
    """
    if len(daily_bars) < 2:
        return None
    trs: list[float] = []
    prev_close = float(daily_bars[0]["close"])
    for b in daily_bars[1:]:
        hi, lo, cl = float(b["high"]), float(b["low"]), float(b["close"])
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        prev_close = cl
    if not trs:
        return None
    window = trs[-period:]
    atr = sum(window) / len(window)
    last_close = float(daily_bars[-1]["close"])
    if last_close <= 0:
        return None
    return atr / last_close
