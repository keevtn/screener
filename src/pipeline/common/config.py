"""Versioned prediction config (docs/ROADMAP.md task 4.3, invariant I3).

ALL signal tunables live in one params blob: horizon/threshold (the contract
constants), decay half-lives, tier weights, model blend weights, signal
thresholds, high-alert cutoff, min items, and the catalyst-armed-drift settings.
The loader is content-addressed: identical params resolve to the same immutable
config_version; any change creates a NEW version (config rows never mutate — I3).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.common.models import Config, params_hash
from pipeline.common.timeutil import utcnow

# Config v1. Every constant in prediction-contract-v1.md §7 plus the Phase-4
# aggregation/signal knobs. Frozen once ~50–100 predictions have graded (Gate 5).
DEFAULT_PARAMS_V1: dict[str, Any] = {
    # --- prediction contract (0.1 / grader) ---
    "horizon_trading_days": 3,
    "threshold": 0.02,  # theta: market-adjusted band
    "benchmark_symbol": "SPY",
    "close_time": "16:00",
    "exchange_tz": "America/New_York",
    "calendar_source": "benchmark_bars",
    # --- window / decay (4.1) ---
    "half_life_hours": {"structured": 48.0, "social": 24.0},
    "tier_weights": {"0": 1.0, "1": 0.9, "2": 0.7, "3": 0.4},
    # --- sentiment blend (I7); text-kind refines weights (3.3) ---
    "blend_weights": {"finbert": 0.6, "lm": 0.4},
    "text_kind_blend": {
        "filing": {"finbert": 0.35, "lm": 0.65},  # L-M higher on filings
        "press_release": {"finbert": 0.5, "lm": 0.5},
        "article": {"finbert": 0.7, "lm": 0.3},  # FinBERT higher on prose
    },
    # --- signal thresholds (4.2) ---
    "sentiment_threshold": 0.15,  # |composite| must reach this to be directional
    "materiality_threshold": 0.50,
    "min_items": 2,
    "high_alert_cutoff": 0.70,
    # Don't re-emit for the same ticker+config within this window (the scheduler
    # re-evaluates every cycle; a persistent signal must not spam the ledger).
    "cooldown_hours": 24.0,
    # Measurement window (Gate 5): freeze config v1 except bugfixes until this many
    # structured predictions have graded (owner set 25–50, was roadmap's 50–100).
    "measurement_window": {"min_graded": 25, "target_graded": 50},
    # --- catalyst-armed drift, PEAD (4.5) ---
    "armed": {
        "enabled_types": ["earnings_results"],
        "reaction_threshold": 0.02,  # |market-adj reaction| to fire a continuation
        "ttl_hours": 96.0,
    },
}


def config_version_id(params: dict[str, Any]) -> str:
    """Deterministic, content-addressed version id."""
    return f"cfg-{params_hash(params)[:12]}"


def get_or_create_config(
    session: Session,
    params: dict[str, Any] | None = None,
    *,
    notes: str = "config v1",
) -> Config:
    """Return the immutable config for these params, creating a version if new (I3)."""
    params = params if params is not None else DEFAULT_PARAMS_V1
    ph = params_hash(params)
    existing = session.execute(select(Config).where(Config.params_hash == ph)).scalar_one_or_none()
    if existing is not None:
        return existing
    cfg = Config(
        config_version=config_version_id(params),
        params_json=params,
        params_hash=ph,
        created_at=utcnow(),
        notes=notes,
    )
    session.add(cfg)
    session.commit()
    return cfg
