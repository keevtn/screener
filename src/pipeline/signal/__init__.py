"""Signal engine: window composites -> contract-conformant predictions (Phase 4)."""

from pipeline.signal.armed import (
    arm_reaction_dependent,
    arm_ticker,
    market_adjusted_reaction,
    resolve_all_armed,
    resolve_armed_state,
)
from pipeline.signal.cycle import console_alert, run_signal_cycle, webhook_alert
from pipeline.signal.engine import SignalEngine, evaluate_window

__all__ = [
    "SignalEngine",
    "arm_reaction_dependent",
    "arm_ticker",
    "console_alert",
    "evaluate_window",
    "market_adjusted_reaction",
    "resolve_all_armed",
    "resolve_armed_state",
    "run_signal_cycle",
    "webhook_alert",
]
