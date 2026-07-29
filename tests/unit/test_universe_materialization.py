"""Task 0.6 gate tests: criteria filter, provider stamp, fallback, diff gating."""

from __future__ import annotations

from pipeline.marketdata.finviz import FinvizAuthError, FundamentalsRow
from pipeline.marketdata.universe import apply_criteria, compute_diff, materialize

CFG = {
    "criteria": {
        "countries": ["USA"],
        "market_cap_min": 2_000_000_000,
        "price_min": 1.00,
        "avg_dollar_volume_min": 1_000_000,
    },
    "diff_review_threshold": 0.10,
}


def _rows():
    return [
        FundamentalsRow("BIGCAP", market_cap=5e9, price=120.0, avg_volume=2_000_000, country="USA"),
        FundamentalsRow("SMALLCAP", market_cap=9e8, price=8.0, avg_volume=500_000, country="USA"),
        FundamentalsRow("PENNY", market_cap=3e9, price=0.75, avg_volume=1e7, country="USA"),
        FundamentalsRow("THIN", market_cap=4e9, price=50.0, avg_volume=5_000, country="USA"),
        FundamentalsRow(
            "FOREIGN", market_cap=6e9, price=40.0, avg_volume=3_000_000, country="China"
        ),
    ]


class FakeFinviz:
    name = "finviz"

    def __init__(self, rows=None, exc=None):
        self._rows = rows or []
        self._exc = exc

    def fetch_fundamentals(self):
        if self._exc:
            raise self._exc
        return self._rows


class FakeSymDir:
    name = "symbol_directory"

    def __init__(self, symbols):
        self._symbols = symbols

    def fetch_symbols(self):
        return self._symbols


# --- criteria ---------------------------------------------------------------


def test_universe_criteria_filter():
    members = apply_criteria(_rows(), CFG, watchlist=["WATCHME"])
    # BIGCAP passes all; SMALLCAP (cap), PENNY (price), THIN (dollar-vol),
    # FOREIGN (country) each fail one floor. Watchlist is always included.
    assert members == {"BIGCAP", "WATCHME"}


def test_watchlist_forced_in_even_if_criteria_would_exclude():
    members = apply_criteria(_rows(), CFG, watchlist=["PENNY"])
    assert "PENNY" in members  # would fail price floor, but watchlisted


# --- diff -------------------------------------------------------------------


def test_compute_diff_added_removed_fraction():
    diff = compute_diff({"A", "B", "C", "D"}, {"B", "C", "D", "E"})
    assert diff["added"] == ["E"]
    assert diff["removed"] == ["A"]
    assert diff["fraction"] == 0.5  # (1 + 1) / 4


# --- provider chain + gating ------------------------------------------------


def test_snapshot_provider_stamped_finviz():
    result = materialize(
        finviz=FakeFinviz(rows=_rows()),
        symbol_dir=FakeSymDir([]),
        cfg=CFG,
        watchlist=["WATCHME"],
        previous_members=[],
    )
    assert result.provider == "finviz"
    assert result.status == "applied"  # first snapshot always applies
    assert set(result.members) == {"BIGCAP", "WATCHME"}


def test_fallback_provider_activates_and_is_stamped():
    result = materialize(
        finviz=FakeFinviz(exc=FinvizAuthError("expired")),
        symbol_dir=FakeSymDir(["MSFT", "TSLA"]),
        cfg=CFG,
        watchlist=["AAPL"],
        previous_members=["MSFT", "GOOG"],
        previous_provider="finviz",
    )
    assert result.provider == "symbol_directory"
    # watchlist + prior members still listed: AAPL + (MSFT,GOOG ∩ MSFT,TSLA) = MSFT.
    assert set(result.members) == {"AAPL", "MSFT"}
    assert "finviz unavailable" in (result.notes or "")


def test_universe_diff_threshold_flags_pending_review():
    previous = [f"T{i}" for i in range(100)]
    # Members: keep T0..T49, add N0..N49 -> 50 removed + 50 added = 100% diff.
    rows = [FundamentalsRow(f"T{i}") for i in range(50)] + [
        FundamentalsRow(f"N{i}") for i in range(50)
    ]
    result = materialize(
        finviz=FakeFinviz(rows=rows),
        symbol_dir=FakeSymDir([]),
        cfg={"diff_review_threshold": 0.10},  # empty criteria -> every row is a member
        watchlist=[],
        previous_members=previous,
        previous_provider="finviz",
    )
    assert result.diff["fraction"] == 1.0
    assert result.status == "pending_review"  # not auto-applied


def test_small_diff_auto_applies():
    previous = [f"T{i}" for i in range(100)]
    rows = [FundamentalsRow(f"T{i}") for i in range(100)] + [FundamentalsRow("NEW")]
    result = materialize(
        finviz=FakeFinviz(rows=rows),
        symbol_dir=FakeSymDir([]),
        cfg={"diff_review_threshold": 0.10},
        watchlist=[],
        previous_members=previous,
        previous_provider="finviz",
    )
    assert result.diff["fraction"] == 0.01
    assert result.status == "applied"


def test_provider_switch_forces_review():
    # Even a tiny diff parks for review when the provider changed.
    result = materialize(
        finviz=FakeFinviz(exc=FinvizAuthError("x")),
        symbol_dir=FakeSymDir(["A", "B"]),
        cfg={"diff_review_threshold": 0.99},
        watchlist=["A"],
        previous_members=["A", "B"],
        previous_provider="finviz",
    )
    assert result.provider == "symbol_directory"
    assert result.status == "pending_review"
    assert "provider switch" in (result.notes or "")
