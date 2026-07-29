"""Run the ranker on demand or on a schedule (docs/ROADMAP.md task 7.2).

The daily/weekly cron uses --trigger; the frontend force-run button hits the API
instead (same run_ranking underneath). Model + timeframe are selectable here too.

    python scripts/run_ranker.py                       # manual, defaults
    python scripts/run_ranker.py --trigger scheduled_daily
    python scripts/run_ranker.py --trigger premarket   # PMR overlay: overnight scope
    python scripts/run_ranker.py --model opus-4-8 --horizon 5

--trigger premarket scopes candidates to the PMR overnight window (prev close ->
now). ranking_runs.trigger keeps its 3-value CHECK contract — premarket runs are
recorded as scheduled_daily and tagged via filter_json["scope"] = "premarket".
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.agents import (
    DEFAULT_RANKER_MODEL,
    build_candidate_filter,
    default_client,
    default_ranker_candidates,
    run_ranking,
)
from pipeline.common.config import get_or_create_config
from pipeline.common.db import make_engine
from pipeline.common.models import Ranking


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    parser.add_argument("--model", default=DEFAULT_RANKER_MODEL)
    parser.add_argument("--horizon", type=int, default=None, help="trading-day timeframe")
    parser.add_argument(
        "--trigger",
        default="manual",
        choices=["manual", "scheduled_daily", "scheduled_weekly", "premarket"],
    )
    parser.add_argument("--limit", type=int, default=default_ranker_candidates())
    args = parser.parse_args()

    db_trigger, filter_spec = args.trigger, None
    if args.trigger == "premarket":
        db_trigger = "scheduled_daily"  # CHECK-constraint contract; scope tag below
        filter_spec = build_candidate_filter()
        filter_spec["scope"] = "premarket"
        try:
            from datetime import timedelta

            from pipeline.common.timeutil import utcnow
            from pipeline.marketdata import MarketDataProvider, TradingCalendar
            from pipeline.panel import premarket_window

            end_d = utcnow().date()
            spy = MarketDataProvider().get_benchmark_bars(end_d - timedelta(days=45), end_d)
            start, end = premarket_window(TradingCalendar.from_bars(spy), utcnow())
            filter_spec["window_start"] = start.isoformat()
            # Fractional days; floor keeps a just-after-close run non-degenerate.
            filter_spec["recency_days"] = max((end - start).total_seconds() / 86400.0, 0.25)
        except Exception as exc:  # noqa: BLE001 — calendar is best-effort here
            print(f"premarket window unavailable ({exc}); using 1-day recency")
            filter_spec["recency_days"] = 1.0

    engine = make_engine(args.url)
    client = default_client()  # CLAUDE_API=true -> API credits; false -> Claude plan
    with Session(engine) as session:
        cfg = get_or_create_config(session)
        run = run_ranking(
            session,
            client,
            params=cfg.params_json,
            config_version=cfg.config_version,
            filter_spec=filter_spec,
            model=args.model,
            horizon_trading_days=args.horizon,
            trigger=db_trigger,
            # Opus allowed only on a deliberate on-demand run (--trigger manual);
            # scheduled_daily/weekly/premarket are automated and never use Opus.
            explicit_model=(args.trigger == "manual"),
            limit=args.limit,
        )
        items = (
            session.execute(
                select(Ranking).where(Ranking.run_id == run.run_id).order_by(Ranking.rank)
            )
            .scalars()
            .all()
        )
        print(
            f"run={run.run_id} status={run.status} model={run.model} "
            f"candidates={run.candidate_count} ranked={len(items)}"
        )
        for r in items:
            print(
                f"  {r.rank:2}. {r.ticker:6} {r.direction:8} "
                f"conv={r.conviction:.2f}  {r.rationale[:70]}"
            )


if __name__ == "__main__":
    main()
