"""Run the weekly analyst (docs/ROADMAP.md task 7.3).

Reads the graded ledger, writes a pending_changes proposal (report + optional
config patch). Approve/reject it with scripts/approve.py — this script never
changes config.

    python scripts/run_analyst.py [--model opus-4-8]
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

from pipeline.agents import DEFAULT_ANALYST_MODEL, default_client, run_analyst
from pipeline.common.config import get_or_create_config
from pipeline.common.db import make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    parser.add_argument("--model", default=DEFAULT_ANALYST_MODEL)
    args = parser.parse_args()

    engine = make_engine(args.url)
    client = default_client()  # CLAUDE_API=true -> API credits; false -> Claude plan
    with Session(engine) as session:
        cfg = get_or_create_config(session)
        change = run_analyst(
            session,
            client,
            base_config_version=cfg.config_version,
            params=cfg.params_json,
            model=args.model,
        )
        if change is None:
            print("analyst produced no valid proposal (logged as failed spend)")
            return
        # Read attributes while still bound to the session.
        change_id, status, patch = change.id, change.status, change.patch_json
    print(f"proposal {change_id} created (status={status})")
    print(f"  patch: {patch}")
    print(f"  review with: python scripts/approve.py show {change_id}")


if __name__ == "__main__":
    main()
