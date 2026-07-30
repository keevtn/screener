"""Assign exit policies to the standing paper-trader's configs (the vol_stop A/B).

The exp-* configs ship (in the seed) with no exit_policy = horizon_hold. To run a
real day-one comparison of a NON-horizon exit against the baseline, this assigns
``vol_stop`` to a SUBSET of them and leaves the rest on horizon_hold. Split by
judgment (see A/B below); everything else — loss caps, flatten backstop, paper
assert — wraps BOTH policies unchanged.

Idempotent + walk-forward safe: a config is only (re)assigned when it currently
has NO exit_policy AND has no trades yet, so this never retro-tunes a config that
has already started racing (which would break the frozen-params discipline). On a
fresh account it sets them once; on later boots it's a no-op.

    python scripts/assign_exit_policies.py [--url DATABASE_URL]

Order-independent of the driver: safe to run whether or not TRADER_DRIVER_ENABLED
is set — it only edits config rows.
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.db import make_engine
from pipeline.common.models import SimConfig, SimTrade

# The vol_stop arm (adverse stop = atr_mult × recent daily ATR fraction, horizon-
# backstopped). atr_mult 2.0 is the framework default — a wide protective stop.
_VOL_STOP = {"kind": "vol_stop", "atr_mult": 2.0}

# A/B split (see the config's rationale):
#   vol_stop  : broad intraday finbert (finbert-direction), binary/vol event names
#               where cutting adverse moves matters most (fda-binary, offering-short),
#               and volatile small caps that ATR-scaling suits (smallcap-material).
#   horizon   : the complement — incl. the hold-to-deal M&A thesis (ma-target-hold)
#               and a matched broad-intraday-finbert control (any-material).
VOL_STOP_CONFIGS = {
    "exp-finbert-direction",
    "exp-fda-binary",
    "exp-offering-short",
    "exp-smallcap-material",
}


def assign(session: Session) -> dict[str, str]:
    """Return {config_name: action} for logging. Actions: assigned | skip-has-policy
    | skip-has-trades | absent."""
    actions: dict[str, str] = {}
    for name in sorted(VOL_STOP_CONFIGS):
        cfg = session.execute(
            select(SimConfig).where(SimConfig.name == name)
        ).scalar_one_or_none()
        if cfg is None:
            actions[name] = "absent"
            continue
        params = dict(cfg.params_json or {})
        if params.get("exit_policy") is not None:
            actions[name] = "skip-has-policy"
            continue
        trades = session.execute(
            select(func.count()).select_from(SimTrade).where(SimTrade.config_id == cfg.config_id)
        ).scalar_one()
        if trades > 0:
            actions[name] = "skip-has-trades"  # already racing — don't retro-tune
            continue
        params["exit_policy"] = dict(_VOL_STOP)
        cfg.params_json = params  # reassign so SQLAlchemy tracks the JSON change
        actions[name] = "assigned"
    session.commit()
    return actions


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=None, help="database URL (default: $DATABASE_URL)")
    args = ap.parse_args()
    engine = make_engine(args.url)
    with Session(engine) as session:
        actions = assign(session)
    assigned = [n for n, a in actions.items() if a == "assigned"]
    print(
        f"[exit-policies] vol_stop assigned to {len(assigned)}/{len(VOL_STOP_CONFIGS)} configs: "
        f"{assigned or '(none — already set or trading)'}"
    )
    for name, action in sorted(actions.items()):
        print(f"[exit-policies]   {name:<26} {action}")


if __name__ == "__main__":
    main()
