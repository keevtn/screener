"""Daily Google-Trends search-interest snapshot for the bounded hot set.

A NEW descriptive attention axis (shadow-only). Snapshots each hot-set ticker's
own-normalized search-interest series into search_interest_daily, once per day.
Fail-soft, paced, kill-switchable via SEARCH_TRENDS_ENABLED. Its own supervised
window in start.ps1 (the paced queries take minutes — decoupled from the fast
sweep loop).

    python scripts/run_search_trends.py --once            # one snapshot, exit
    python scripts/run_search_trends.py --loop            # snapshot once/day, sleep
    python scripts/run_search_trends.py --once --limit 20 # smaller hot set
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
except ImportError:
    pass

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from pipeline.common.db import make_engine  # noqa: E402
from pipeline.common.models import Base, SearchInterestDaily  # noqa: E402
from pipeline.common.timeutil import utcnow  # noqa: E402
from pipeline.ingest.trends import (  # noqa: E402
    GoogleTrendsClient,
    hot_set,
    search_trends_enabled,
    snapshot_search_interest,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("search_trends")


def _done_today(engine) -> bool:
    with Session(engine) as s:
        last = s.execute(select(func.max(SearchInterestDaily.updated_at))).scalar_one_or_none()
    return last is not None and last.date() == utcnow().date()


def run_once(engine, *, limit: int) -> None:
    Base.metadata.create_all(engine)  # ensure search_interest_daily exists on a long-lived DB
    if not search_trends_enabled():
        log.info("search-trends disabled (SEARCH_TRENDS_ENABLED=0) — skipping")
        return
    with Session(engine) as s:
        tickers = hot_set(s, limit=limit)
        if not tickers:
            log.warning("search-trends: empty hot set (no recent catalysts / watchlist) — skip")
            return
        log.info("search-trends snapshot: %d tickers (hot set) ...", len(tickers))
        stats = snapshot_search_interest(s, tickers, GoogleTrendsClient())
    log.info(
        "search-trends done: %d tickers, %d rows, %d failed, %d rate-limited",
        stats["tickers"], stats["rows"], stats["failed"], stats["rate_limited"],
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--url", default=None)
    args = ap.parse_args()
    engine = make_engine(args.url)

    if args.loop:
        log.info("search-trends loop: one snapshot/day, checking every 6h")
        while True:
            try:
                if not _done_today(engine):
                    run_once(engine, limit=args.limit)
                else:
                    log.info("search-trends: already snapshotted today — sleeping")
            except Exception:  # noqa: BLE001 — the daily loop never dies on a fault
                log.warning("search-trends run errored (continuing)", exc_info=True)
            time.sleep(6 * 3600)
    else:
        run_once(engine, limit=args.limit)


if __name__ == "__main__":
    main()
