"""Run one signal cycle over the current DB (docs/ROADMAP.md task 4.4).

Evaluates tickers with fresh clusters, emits structured predictions, resolves
catalyst-armed drift (needs market data), and alerts. Assumes enrichment (Phase 2)
and scoring (Phase 3) have already populated clusters + cluster_scores.

    python scripts/run_signal.py [--url DATABASE_URL]

Set ALERT_WEBHOOK_URL in the environment to also POST each prediction.
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

from sqlalchemy.orm import Session

from pipeline.common.config import get_or_create_config
from pipeline.common.db import make_engine
from pipeline.marketdata import MarketDataProvider
from pipeline.signal.cycle import console_alert, run_signal_cycle, webhook_alert


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    parser.add_argument("--no-armed", action="store_true", help="skip armed-drift (no market data)")
    args = parser.parse_args()

    engine = make_engine(args.url)
    webhook = os.environ.get("ALERT_WEBHOOK_URL")
    alert = webhook_alert(webhook) if webhook else console_alert
    provider = None if args.no_armed else MarketDataProvider()

    with Session(engine) as session:
        cfg = get_or_create_config(session)
        config_version = cfg.config_version
        preds = run_signal_cycle(
            session, cfg.params_json, config_version, provider=provider, alert=alert
        )
        # Capture inside the session (rows detach on close).
        summary = [(p.ticker, p.direction, p.confidence, p.evidence_json) for p in preds]

    print(f"emitted {len(summary)} prediction(s) under {config_version}")
    for ticker, direction, confidence, evidence in summary:
        print(f"  {ticker} {direction} conf={confidence} evidence={evidence}")


if __name__ == "__main__":
    main()
