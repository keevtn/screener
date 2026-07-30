"""Defensive numeric coercion (pipeline.common.nums.to_float) — the choke point
that stopped a TEXT value in a REAL column (a timestamp shifted into the
fundamentals `price` column) from reaching the API as a crashing JSON string.

Also asserts a text value can physically live in a Float column under SQLite and
that to_float neutralizes it — a regression guard for the UNIVERSE panel crash.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from pipeline.common.models import FundamentalsSnapshot
from pipeline.common.nums import to_float


@pytest.mark.parametrize(
    "value,expected",
    [
        (123.45, 123.45),
        (0, 0.0),
        (-7, -7.0),
        ("123.45", 123.45),        # numeric strings are recovered
        ("  -3.5 ", -3.5),
        ("2026-07-22 14:21:40.990779", None),  # the actual bug value -> null
        ("", None),
        ("   ", None),
        ("abc", None),
        (None, None),
        (True, None),              # bools are not numbers here
        (False, None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
    ],
)
def test_to_float(value, expected):
    assert to_float(value) == expected


def test_text_survives_in_float_column_and_is_neutralized(session):
    """SQLite REAL affinity stores an unconvertible string as TEXT; the ORM then
    reads it back as a str. to_float(f.price) must turn that into None (not a
    string that would crash a frontend toFixed)."""
    now = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)
    session.add(FundamentalsSnapshot(
        ticker="NVDA", as_of=date(2026, 7, 22), provider="test",
        market_cap=5_000_000.0, price=100.0, change_pct=0.02, created_at=now,
    ))
    session.commit()
    # Simulate the positional-copy corruption directly: put a timestamp string in
    # the numeric price column via raw SQL (bypassing the ORM's float binding).
    session.execute(text(
        "UPDATE fundamentals_snapshots SET price = :ts WHERE ticker = 'NVDA'"
    ), {"ts": "2026-07-22 14:21:40.990779"})
    session.commit()
    session.expire_all()

    f = session.get(FundamentalsSnapshot, ("NVDA", date(2026, 7, 22)))
    assert isinstance(f.price, str)          # the column really holds TEXT now
    assert to_float(f.price) is None         # ...and the API would emit null, not crash
    assert to_float(f.market_cap) == 5_000_000.0  # unaffected numeric fields survive
