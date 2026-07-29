"""Materialize the tradeable universe (docs/ROADMAP.md task 0.6).

Runs the provider chain (Finviz Elite primary -> Nasdaq symbol directory
fallback), writes a dated, provider-stamped universe snapshot + a fundamentals
snapshot, and — only when the snapshot is 'applied' (not parked for review) —
sets entities.active to the new membership.

Usage:
    python scripts/snapshot_universe.py [--url DATABASE_URL] [--as-of YYYY-MM-DD]

FINVIZ_AUTH_TOKEN is read from the environment (I9).
"""

from __future__ import annotations

import argparse
import os
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from pipeline.common.config_files import load_universe, load_watchlist
from pipeline.common.db import make_engine
from pipeline.common.models import Entity, FundamentalsSnapshot, UniverseSnapshot
from pipeline.common.timeutil import utcnow
from pipeline.marketdata import (
    FinvizProvider,
    SymbolDirectoryProvider,
    materialize,
)


def _latest_snapshot(session: Session) -> UniverseSnapshot | None:
    return (
        session.execute(
            select(UniverseSnapshot)
            .where(UniverseSnapshot.status == "applied")
            .order_by(UniverseSnapshot.snapshot_date.desc(), UniverseSnapshot.created_at.desc())
        )
        .scalars()
        .first()
    )


def persist_snapshot(session: Session, result, as_of: date) -> UniverseSnapshot:
    now = utcnow()
    snap = UniverseSnapshot(
        snapshot_date=as_of,
        provider=result.provider,
        status=result.status,
        members_json=result.members,
        diff_json=result.diff,
        created_at=now,
        notes=result.notes,
    )
    session.add(snap)
    for row in result.fundamentals:
        session.merge(
            FundamentalsSnapshot(
                ticker=row.ticker,
                as_of=as_of,
                provider=result.provider,
                market_cap=row.market_cap,
                shares_float=row.shares_float,
                short_float=row.short_float,
                insider_own=row.insider_own,
                inst_own=row.inst_own,
                avg_volume=row.avg_volume,
                beta=row.beta,
                sector=row.sector,
                industry=row.industry,
                created_at=now,
            )
        )

    if result.status == "applied":
        members = set(result.members)
        for (ticker,) in session.execute(select(Entity.ticker)).all():
            session.execute(
                update(Entity).where(Entity.ticker == ticker).values(active=ticker in members)
            )
    session.commit()
    return snap


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="database URL (default: $DATABASE_URL)")
    parser.add_argument("--as-of", default=None, help="snapshot date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else utcnow().date()
    cfg = load_universe()
    watchlist = load_watchlist()

    finviz = FinvizProvider(os.environ.get("FINVIZ_AUTH_TOKEN", ""))
    symbol_dir = SymbolDirectoryProvider()

    engine = make_engine(args.url)
    with Session(engine) as session:
        prev = _latest_snapshot(session)
        result = materialize(
            finviz=finviz,
            symbol_dir=symbol_dir,
            cfg=cfg,
            watchlist=watchlist,
            previous_members=prev.members_json if prev else [],
            previous_provider=prev.provider if prev else None,
        )
        persist_snapshot(session, result, as_of)

    print(
        f"universe snapshot {as_of} via {result.provider}: {len(result.members)} members, "
        f"status={result.status}, diff={result.diff['fraction']:.1%}"
    )
    if result.status == "pending_review":
        print("  PARKED for human review (not applied to entities.active).")


if __name__ == "__main__":
    main()
