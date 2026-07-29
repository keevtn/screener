"""Gate 5b earnings feed: parse, Finviz-primary/yfinance-fallback snapshot, upsert."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import ScheduledEvent
from pipeline.panel import scheduled_panel
from pipeline.panel.earnings import parse_earnings_date, parse_finviz_earnings, snapshot_earnings

REF = date(2026, 7, 12)
NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)


def test_parse_earnings_date_formats():
    assert parse_earnings_date("7/24/2026", ref=REF) == date(2026, 7, 24)
    assert parse_earnings_date("Jul 24 AMC", ref=REF) == date(2026, 7, 24)
    assert parse_earnings_date("Aug 5/b", ref=REF) == date(2026, 8, 5)
    assert parse_earnings_date("-", ref=REF) is None
    assert parse_earnings_date(None, ref=REF) is None
    # A month already ~10 months past this year rolls to next year.
    assert parse_earnings_date("Jan 5", ref=date(2026, 11, 1)) == date(2027, 1, 5)


def test_parse_finviz_earnings_header_detection():
    csv_with = "Ticker,Company,Earnings Date\nAAPL,Apple,7/31/2026\nMSFT,Microsoft,7/24/2026\n"
    got = parse_finviz_earnings(csv_with, ref=REF)
    assert got == {"AAPL": date(2026, 7, 31), "MSFT": date(2026, 7, 24)}
    # No earnings column -> empty (caller falls back to yfinance).
    assert parse_finviz_earnings("Ticker,Company\nAAPL,Apple\n", ref=REF) == {}


class _FakeFinviz:
    def fetch_earnings(self, *, ref):
        return {"AAPL": date(2026, 7, 31)}  # AAPL from Finviz; MSFT not present


def _fake_yf(ticker, *, ref):
    return {"MSFT": date(2026, 7, 24)}.get(ticker)  # MSFT via yfinance fallback


def test_snapshot_earnings_finviz_then_yfinance(engine):
    with Session(engine) as s:
        stats = snapshot_earnings(
            s,
            ["AAPL", "MSFT", "NADA"],
            finviz_provider=_FakeFinviz(),
            yfinance_lookup=_fake_yf,
            now=NOW,
        )
        assert stats.finviz_hits == 1 and stats.yfinance_hits == 1
        assert stats.upserted == 2  # NADA has no date anywhere -> skipped

        events = {
            e.ticker: (e.event_date, e.source) for e in s.execute(select(ScheduledEvent)).scalars()
        }
        assert events["AAPL"] == (date(2026, 7, 31), "finviz")
        assert events["MSFT"] == (date(2026, 7, 24), "yfinance")

        # The scheduled panel serves them with countdowns.
        panel = scheduled_panel(s, now=NOW)
        by_ticker = {p["ticker"]: p["days_until"] for p in panel}
        assert by_ticker["MSFT"] == 12 and by_ticker["AAPL"] == 19


def test_snapshot_idempotent(engine):
    with Session(engine) as s:
        snapshot_earnings(
            s, ["AAPL"], finviz_provider=_FakeFinviz(), yfinance_lookup=_fake_yf, now=NOW
        )
        snapshot_earnings(
            s, ["AAPL"], finviz_provider=_FakeFinviz(), yfinance_lookup=_fake_yf, now=NOW
        )
        count = len(
            s.execute(select(ScheduledEvent).where(ScheduledEvent.ticker == "AAPL")).scalars().all()
        )
        assert count == 1  # upsert on (ticker, type, date)
