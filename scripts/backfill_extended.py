"""Backfill extended_session_daily from the intraday parquet already on disk.

One-shot seed so the PREMARKET/EXTENDED tracker has some history immediately instead
of only accruing forward. Reads the cached prepost bars (yfinance <t>_1d.parquet with
the extended flag; the Alpaca minute cache <t>.parquet, ET-derived) and the daily bar
cache for regular-session prices. Idempotent: create_all makes the table if missing,
and the per-(ticker,date) upsert is safe to re-run.

Honest coverage: only days genuinely present in the intraday cache get extended fields
(yfinance keeps ~days of 1m data, so this is thin history); every other day accrues
forward once the loop's extended steps run.

    python scripts/backfill_extended.py               # uses DATABASE_URL / default DB
    python scripts/backfill_extended.py --cache-dir data/bars_intraday
"""

from __future__ import annotations

import argparse
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
except ImportError:
    pass

from sqlalchemy.orm import Session  # noqa: E402

from pipeline.common.db import make_engine  # noqa: E402
from pipeline.common.models import Base  # noqa: E402
from pipeline.marketdata import MarketDataProvider  # noqa: E402
from pipeline.marketdata.extended import backfill_from_cache  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", default="data/bars_intraday")
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    engine = make_engine(args.url)
    Base.metadata.create_all(engine)  # ensure extended_session_daily exists (idempotent)
    provider = MarketDataProvider()

    def daily_bars_fn(t, start, end):
        df = provider.get_daily_bars(t, start, end)
        out = {}
        for _, row in df.iterrows():
            d = row["date"].date() if hasattr(row["date"], "date") else row["date"]
            out[d] = {"open": float(row["open"]), "close": float(row["adj_close"])}
        return out

    with Session(engine) as s:
        result = backfill_from_cache(s, daily_bars_fn=daily_bars_fn, cache_dir=args.cache_dir)
    print(result)


if __name__ == "__main__":
    main()
