"""Snapshot upcoming earnings into scheduled_events (docs/ROADMAP.md task 5b.1).

Finviz Elite export (primary, one bulk request) with a per-ticker yfinance fallback.
Universe = configs/watchlist.txt + active entities. Dates are approximate. Run
DAILY (own cron, or via run_pipeline.py which rolls statuses each cycle).

    python scripts/snapshot_events.py [--url DATABASE_URL]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.config_files import load_watchlist
from pipeline.common.db import make_engine
from pipeline.common.models import Entity
from pipeline.panel import FinvizEarningsProvider, roll_event_status, snapshot_earnings


def universe(session: Session) -> list[str]:
    active = session.execute(select(Entity.ticker).where(Entity.active.is_(True))).scalars().all()
    return sorted(set(load_watchlist()) | set(active))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    parser.add_argument("--no-finviz", action="store_true", help="yfinance only")
    args = parser.parse_args()

    token = os.environ.get("FINVIZ_AUTH_TOKEN", "").strip()
    provider = None if (args.no_finviz or not token) else FinvizEarningsProvider(token)

    engine = make_engine(args.url)
    with Session(engine) as session:
        tickers = universe(session)
        stats = snapshot_earnings(session, tickers, finviz_provider=provider)
        rolled = roll_event_status(session)
    print(
        f"universe={stats.universe} finviz={stats.finviz_hits} yfinance={stats.yfinance_hits} "
        f"upserted={stats.upserted} rolled_passed={rolled}"
    )
    if stats.finviz_error:
        print(f"  finviz unavailable (fell back to yfinance): {stats.finviz_error}")


if __name__ == "__main__":
    main()
