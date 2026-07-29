"""Run a single-ticker deep dive on demand (docs/ROADMAP.md task 7.4).

The frontend DEEP DIVE button hits the API (POST /tickers/{t}/analyze); this is the
CLI equivalent (same run_deep_dive underneath). Own-data only — no internet. Model +
timeframe are selectable; rate limit + daily cap apply.

    python scripts/run_deep_dive.py AAPL
    python scripts/run_deep_dive.py NVDA --model opus-4-8 --horizon 5
    python scripts/run_deep_dive.py TSLA --no-rate-limit     # bypass the window (ops)
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from sqlalchemy.orm import Session

from pipeline.agents import DEFAULT_DEEP_DIVE_MODEL, default_client, run_deep_dive
from pipeline.agents.deepdive import DeepDiveRateLimited
from pipeline.common.config import get_or_create_config
from pipeline.common.db import make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--url", default=None)
    parser.add_argument("--model", default=DEFAULT_DEEP_DIVE_MODEL)
    parser.add_argument("--horizon", type=int, default=None, help="trading-day timeframe")
    parser.add_argument(
        "--no-rate-limit",
        dest="rate_limit",
        action="store_false",
        help="skip the per-window distinct-ticker rate limit (ops only)",
    )
    args = parser.parse_args()

    engine = make_engine(args.url)
    client = default_client()  # CLAUDE_API=true -> API credits; false -> Claude plan
    with Session(engine) as session:
        cfg = get_or_create_config(session)
        try:
            a = run_deep_dive(
                session,
                client,
                args.ticker,
                params=cfg.params_json,
                config_version=cfg.config_version,
                model=args.model,
                horizon_trading_days=args.horizon,
                enforce_rate_limit=args.rate_limit,
            )
        except DeepDiveRateLimited as exc:
            print(f"rate limited: {exc}")
            return

    print(f"analysis={a.analysis_id} ticker={a.ticker} status={a.status} model={a.model}")
    if a.status == "empty":
        print(f"  {a.error}")
        return
    if a.status == "failed":
        print(f"  {a.error}")
        return
    print(f"  direction={a.direction} conviction={a.conviction:.2f}")
    print(f"  thesis: {a.thesis}")
    for e in a.key_evidence_json:
        cid = f" [{e['cluster_id']}]" if e.get("cluster_id") else ""
        print(f"  + {e['point']}{cid}")
    for r in a.risks_json:
        print(f"  ! risk: {r}")
    for w in a.what_would_change_json:
        print(f"  ? would change: {w}")


if __name__ == "__main__":
    main()
