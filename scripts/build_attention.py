"""Rebuild the attention-baseline layer (attention_daily + buzz_baselines).

Rolls up per-ticker daily news volume + sentiment from the live pipeline DB and
(optionally) the legacy import, then recomputes warm-start buzz baselines. Both
steps are idempotent full recomputes -- safe to run daily / on a schedule.

    python scripts/build_attention.py [--url DATABASE_URL] [--legacy data/legacy.db]

The target DB (--url) receives attention_daily + buzz_baselines; --legacy adds the
historical social slice into the rollup for the buzz warm start.
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

from pipeline.aggregate.attention import build_attention_daily, compute_buzz_baselines
from pipeline.common.db import make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="target DB (default: $DATABASE_URL)")
    parser.add_argument("--legacy", default=None, help="legacy SQLite path to fold into the rollup")
    args = parser.parse_args()

    engine = make_engine(args.url)
    sources = [engine]
    if args.legacy:
        sources.append(make_engine(f"sqlite:///{args.legacy}"))

    with Session(engine) as session:
        n_days = build_attention_daily(session, sources)
        n_base = compute_buzz_baselines(session)
    src = "pipeline" + (" + legacy" if args.legacy else "")
    print(f"attention_daily rows: {n_days} (from {src}); buzz_baselines: {n_base} tickers")


if __name__ == "__main__":
    main()
