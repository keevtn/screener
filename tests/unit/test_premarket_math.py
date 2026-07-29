"""PMR unit surface: window math, lean logic, trading-age helper (no DB)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from pipeline.marketdata import CalendarRangeError, TradingCalendar
from pipeline.panel.premarket import (
    _cluster_lean,
    _resolve_lean,
    _trading_age,
    premarket_window,
)

# July 2026: 16 Thu, 17 Fri, 20 Mon, 21 Tue, 22 Wed (EDT, so 16:00 ET = 20:00 UTC).
CAL = TradingCalendar([date(2026, 7, d) for d in (16, 17, 20, 21, 22)])


def test_window_spans_weekend():
    # Monday 08:30 ET premarket reaches back to FRIDAY's close.
    now = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
    start, end = premarket_window(CAL, now)
    assert start == datetime(2026, 7, 17, 20, 0, tzinfo=UTC)
    assert end == now


def test_window_plain_overnight():
    now = datetime(2026, 7, 21, 12, 30, tzinfo=UTC)
    start, _ = premarket_window(CAL, now)
    assert start == datetime(2026, 7, 20, 20, 0, tzinfo=UTC)


def test_prev_trading_day():
    assert CAL.prev_trading_day(date(2026, 7, 22)) == date(2026, 7, 21)
    assert CAL.prev_trading_day(date(2026, 7, 20)) == date(2026, 7, 17)  # over the weekend
    assert CAL.prev_trading_day(date(2026, 7, 19)) == date(2026, 7, 17)  # non-trading input
    with pytest.raises(CalendarRangeError):
        CAL.prev_trading_day(date(2026, 7, 16))  # at/before calendar start


def test_cluster_lean_priorities():
    # Structural prior wins even against opposite sentiment:
    assert _cluster_lean("secondary_offering", "subject", "bullish", 0.9) == "short"
    assert _cluster_lean("ma", "target", None, None) == "long"
    assert _cluster_lean("ma", "acquirer", None, None) is None  # ambiguous role: no lean
    # Classifier hint next:
    assert _cluster_lean("earnings_results", "subject", "bullish", None) == "long"
    assert _cluster_lean("earnings_results", "subject", "bearish", 0.9) == "short"
    # Sentiment sign last, with the conviction bar:
    assert _cluster_lean(None, None, "none", 0.5) == "long"
    assert _cluster_lean(None, None, None, -0.31) == "short"
    assert _cluster_lean(None, None, None, 0.29) is None  # below bar: never coin-flip


def test_resolve_lean():
    assert _resolve_lean([]) == "none"
    assert _resolve_lean([None, None]) == "none"
    assert _resolve_lean(["long", None, "long"]) == "long"
    assert _resolve_lean(["long", "short"]) == "mixed"


def test_trading_age_caps_and_edges():
    assert _trading_age(CAL, date(2026, 7, 21), date(2026, 7, 21)) == 0
    assert _trading_age(CAL, date(2026, 7, 21), date(2026, 7, 22)) == 1
    assert _trading_age(CAL, date(2026, 7, 17), date(2026, 7, 22)) == 2  # capped at 2
    # Live edge: session on the last calendar day — walking off the end is age 0.
    assert _trading_age(CAL, date(2026, 7, 22), date(2026, 7, 22)) == 0
