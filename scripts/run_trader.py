"""Standing paper-trading driver entrypoint (Railway app service).

    python scripts/run_trader.py

Gated by TRADER_DRIVER_ENABLED (default OFF → this script logs and exits, so it
is a safe no-op when the flag isn't set). When ON, it runs the standing daily
loop: arm one session per trading day off the Alpaca clock, sweep entries/exits,
flatten ~10min before the close, roll up the EOD report card — writing
sim_trades / sim_daily_summary to the shared volume DB.

Order placement happens ONLY in this process's internal clock loop. The web API
never imports the trade path. Disable trading instantly by unsetting
TRADER_DRIVER_ENABLED and restarting the service.

Uses DATABASE_URL from the environment (default sqlite:///data/pipeline.db) — on
Railway point at the mounted volume so the ledger survives redeploys, which is
what makes a mid-market restart safe (see pipeline.sim.driver).
"""

from __future__ import annotations

import logging
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from pipeline.common.db import make_engine
from pipeline.sim.driver import driver_enabled, run_trader_driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("run_trader")


def main() -> None:
    if not driver_enabled():
        log.info("TRADER_DRIVER_ENABLED is off — driver not started (no-op).")
        return
    # 30s busy timeout: the driver shares pipeline.db with the API + pipeline loop;
    # wait out a write-lock burst rather than crash the standing loop.
    engine = make_engine(busy_timeout_ms=30000)
    log.info("TRADER driver: enabled — starting standing daily loop.")
    run_trader_driver(engine=engine)


if __name__ == "__main__":
    main()
