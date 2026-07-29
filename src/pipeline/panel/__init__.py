"""Catalyst panel + presets — the tool track (docs/ROADMAP.md Phase 5b).

A standalone catalyst intelligence utility: a fired + scheduled catalyst panel and
the preset system that configures the screener, alerts, and (later) the agent
candidate filter. Touches zero signal parameters — sanctioned measurement-window
work.
"""

from pipeline.panel.earnings import (
    FinvizEarningsProvider,
    next_earnings_yfinance,
    parse_earnings_date,
    parse_finviz_earnings,
    snapshot_earnings,
)
from pipeline.panel.premarket import (
    grade_premarket_panels,
    persist_premarket_snapshot,
    premarket_panel,
    premarket_window,
)
from pipeline.panel.presets import compile_preset, load_presets, screen
from pipeline.panel.scheduled import (
    COLD_START_DAYS,
    LOCKUP_DAYS,
    compute_lockup_expiry,
    fired_panel,
    onboard_listing,
    roll_event_status,
    scheduled_panel,
    upsert_scheduled_event,
)

__all__ = [
    "COLD_START_DAYS",
    "LOCKUP_DAYS",
    "FinvizEarningsProvider",
    "compile_preset",
    "compute_lockup_expiry",
    "fired_panel",
    "grade_premarket_panels",
    "load_presets",
    "next_earnings_yfinance",
    "onboard_listing",
    "persist_premarket_snapshot",
    "premarket_panel",
    "premarket_window",
    "parse_earnings_date",
    "parse_finviz_earnings",
    "roll_event_status",
    "scheduled_panel",
    "screen",
    "snapshot_earnings",
    "upsert_scheduled_event",
]
