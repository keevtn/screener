"""Daily fundamentals snapshot for the UNIVERSE screener (docs/ROADMAP.md task 0.6).

Bulk-pulls the Finviz Elite export (sector/industry/market cap/price/volume/float/
short float/ownership/beta/%change) and merges it into fundamentals_snapshots with
today's as_of. Unlike scripts/snapshot_universe.py this does NOT touch
entities.active or universe_snapshots -- it only materializes the fundamentals the
universe screener filters over, so it is safe to run daily on a schedule.

    python scripts/snapshot_fundamentals.py [--url DATABASE_URL] [--as-of YYYY-MM-DD]

FINVIZ_AUTH_TOKEN is read from the environment (I9).
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from sqlalchemy.orm import Session

from pipeline.common.db import make_engine
from pipeline.common.models import FundamentalsSnapshot
from pipeline.common.timeutil import utcnow
from pipeline.marketdata import FinvizProvider


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    parser.add_argument("--as-of", default=None, help="snapshot date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    now = utcnow()
    as_of = date.fromisoformat(args.as_of) if args.as_of else now.date()
    token = os.environ.get("FINVIZ_AUTH_TOKEN", "").strip()
    rows = FinvizProvider(token).fetch_fundamentals()

    engine = make_engine(args.url)
    with Session(engine) as session:
        for r in rows:
            session.merge(
                FundamentalsSnapshot(
                    ticker=r.ticker,
                    as_of=as_of,
                    provider="finviz",
                    market_cap=r.market_cap,
                    shares_float=r.shares_float,
                    short_float=r.short_float,
                    insider_own=r.insider_own,
                    inst_own=r.inst_own,
                    avg_volume=r.avg_volume,
                    beta=r.beta,
                    sector=r.sector,
                    industry=r.industry,
                    price=r.price,
                    change_pct=r.change_pct,
                    created_at=now,
                )
            )
        session.commit()
    print(f"fundamentals snapshot as_of={as_of}: {len(rows)} tickers materialized")


if __name__ == "__main__":
    main()
