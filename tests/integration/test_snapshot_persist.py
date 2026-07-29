"""Task 0.6 gate test: snapshots persist provider-stamped; applied snapshots set active."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from pipeline.common.models import Entity, FundamentalsSnapshot, UniverseSnapshot
from pipeline.marketdata.finviz import FundamentalsRow
from pipeline.marketdata.universe import materialize

CFG = {"criteria": {"market_cap_min": 2e9}, "diff_review_threshold": 0.10}


def _seed_entities(session):
    for t in ("BIGCAP", "SMALLCAP", "WATCHME"):
        session.add(Entity(ticker=t, canonical_name=t, active=True))
    session.commit()


def test_applied_snapshot_stamps_provider_and_sets_active(session):
    from scripts.snapshot_universe import persist_snapshot

    _seed_entities(session)
    rows = [
        FundamentalsRow("BIGCAP", market_cap=5e9, price=120.0, avg_volume=2e6, country="USA"),
        FundamentalsRow("SMALLCAP", market_cap=9e8, price=8.0, avg_volume=5e5, country="USA"),
    ]
    result = materialize(
        finviz=_FakeFinviz(rows),
        symbol_dir=None,
        cfg=CFG,
        watchlist=["WATCHME"],
        previous_members=[],
    )
    persist_snapshot(session, result, date(2025, 3, 12))

    snap = session.execute(select(UniverseSnapshot)).scalars().one()
    assert snap.provider == "finviz"
    assert snap.status == "applied"
    assert set(snap.members_json) == {"BIGCAP", "WATCHME"}

    # Fundamentals rows carry the provider stamp too.
    fund = session.get(FundamentalsSnapshot, ("BIGCAP", date(2025, 3, 12)))
    assert fund is not None and fund.provider == "finviz"
    assert fund.market_cap == 5e9

    # entities.active reflects membership (SMALLCAP excluded -> inactive).
    assert session.get(Entity, "BIGCAP").active is True
    assert session.get(Entity, "WATCHME").active is True
    assert session.get(Entity, "SMALLCAP").active is False


def test_pending_snapshot_does_not_touch_active(session):
    from scripts.snapshot_universe import persist_snapshot

    _seed_entities(session)
    # A large diff off a non-empty previous -> pending_review.
    previous = [f"T{i}" for i in range(100)]
    rows = [FundamentalsRow(f"N{i}", market_cap=5e9) for i in range(100)]
    result = materialize(
        finviz=_FakeFinviz(rows),
        symbol_dir=None,
        cfg=CFG,
        watchlist=[],
        previous_members=previous,
        previous_provider="finviz",
    )
    assert result.status == "pending_review"
    persist_snapshot(session, result, date(2025, 3, 12))

    # Seeded entities keep their prior active state — nothing applied.
    assert session.get(Entity, "BIGCAP").active is True
    assert session.get(Entity, "SMALLCAP").active is True


class _FakeFinviz:
    name = "finviz"

    def __init__(self, rows):
        self._rows = rows

    def fetch_fundamentals(self):
        return self._rows
