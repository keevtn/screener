"""Grade all open predictions against the contract (docs/ROADMAP.md task 0.5).

Usage:
    python scripts/grade.py [--url DATABASE_URL] [--bars data/bars]

Grades every prediction with status='open' whose horizon has fully elapsed;
predictions still inside their horizon (or lacking bars) are left open.
"""

from __future__ import annotations

import argparse

from sqlalchemy.orm import Session

from pipeline.common.db import make_engine
from pipeline.grade import grade_open_predictions
from pipeline.marketdata import MarketDataProvider


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="database URL (default: $DATABASE_URL)")
    parser.add_argument("--bars", default="data/bars", help="parquet bar cache dir")
    args = parser.parse_args()

    engine = make_engine(args.url)
    provider = MarketDataProvider(args.bars)

    with Session(engine) as session:
        graded, skipped = grade_open_predictions(session, provider)

    print(f"graded {graded}, left open {skipped}, of {graded + skipped} open predictions")


if __name__ == "__main__":
    main()
