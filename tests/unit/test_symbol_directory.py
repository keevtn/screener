"""Task 0.6: Nasdaq Trader symbol-directory parsing (fallback provider)."""

from __future__ import annotations

from pathlib import Path

from pipeline.marketdata.symbol_directory import parse_symbol_directory

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "nasdaqlisted_sample.txt"


def test_parses_common_stock_excludes_etf_and_test_issue():
    symbols = parse_symbol_directory(FIXTURE.read_text(encoding="utf-8"))
    assert symbols == ["AAPL", "MSFT"]  # ZXYW (test), QQQ (ETF), footer excluded


def test_empty_input():
    assert parse_symbol_directory("") == []
