"""Universe materialization through the provider chain (docs/ROADMAP.md task 0.6).

Pure orchestration (no DB, no network of its own): apply universe.yaml criteria to
a provider's fundamentals, fall back to the symbol directory when the primary
fails, compute the membership diff, and decide applied-vs-pending-review. The
script scripts/snapshot_universe.py wires this to the providers and the DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from pipeline.marketdata.finviz import (
    FinvizAuthError,
    FinvizProvider,
    FinvizSchemaError,
    FundamentalsRow,
)
from pipeline.marketdata.symbol_directory import SymbolDirectoryProvider

# Failures that trigger fallback to the symbol directory.
_FINVIZ_FAILURES = (FinvizAuthError, FinvizSchemaError, httpx.HTTPError)


@dataclass(frozen=True)
class UniverseResult:
    provider: str
    members: list[str]
    fundamentals: list[FundamentalsRow]
    diff: dict[str, Any]
    status: str  # applied | pending_review
    notes: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


def apply_criteria(
    rows: list[FundamentalsRow], cfg: dict[str, Any], watchlist: list[str] | None = None
) -> set[str]:
    """Select tickers meeting universe.yaml criteria; watchlist always included."""
    crit = cfg.get("criteria", {}) if cfg else {}
    cap_min = crit.get("market_cap_min")
    price_min = crit.get("price_min")
    adv_min = crit.get("avg_dollar_volume_min")
    countries = {c.upper() for c in (crit.get("countries") or [])}

    members: set[str] = set()
    for r in rows:
        if countries and (r.country or "").upper() not in countries:
            continue
        if cap_min is not None and (r.market_cap is None or r.market_cap < cap_min):
            continue
        if price_min is not None and (r.price is None or r.price < price_min):
            continue
        if adv_min is not None:
            adv = (r.avg_volume or 0.0) * (r.price or 0.0)
            if adv < adv_min:
                continue
        members.add(r.ticker)

    members |= {t.upper() for t in (watchlist or [])}
    members |= {t.upper() for t in (cfg.get("always_include") or [])}
    return members


def compute_diff(previous: set[str], members: set[str]) -> dict[str, Any]:
    added = sorted(members - previous)
    removed = sorted(previous - members)
    denom = max(1, len(previous))
    return {
        "added": added,
        "removed": removed,
        "fraction": (len(added) + len(removed)) / denom,
    }


def materialize(
    *,
    finviz: FinvizProvider,
    symbol_dir: SymbolDirectoryProvider,
    cfg: dict[str, Any],
    watchlist: list[str],
    previous_members: list[str] | set[str],
    previous_provider: str | None = None,
) -> UniverseResult:
    """Run the provider chain and gate the result.

    Finviz is tried first; on any recognized failure the symbol directory takes
    over (membership restricted to watchlist + still-listed prior members). A
    membership diff over ``diff_review_threshold`` OR a provider switch parks the
    snapshot in ``pending_review`` instead of applying it — unless there is no
    prior universe to poison (first snapshot always applies).
    """
    previous = {t.upper() for t in previous_members}
    threshold = float(cfg.get("diff_review_threshold", 0.10)) if cfg else 0.10

    notes: str | None = None
    try:
        rows = finviz.fetch_fundamentals()
        provider = finviz.name
        members = apply_criteria(rows, cfg, watchlist)
    except _FINVIZ_FAILURES as exc:
        provider = symbol_dir.name
        rows = []
        symbols = {s.upper() for s in symbol_dir.fetch_symbols()}
        members = {t.upper() for t in watchlist} | (previous & symbols)
        notes = f"finviz unavailable ({type(exc).__name__}); fell back to symbol_directory"

    diff = compute_diff(previous, members)
    provider_switch = previous_provider is not None and provider != previous_provider

    if not previous:
        status = "applied"  # nothing to poison on the first materialization
    elif diff["fraction"] > threshold or provider_switch:
        status = "pending_review"
    else:
        status = "applied"

    if provider_switch and status == "pending_review":
        notes = (notes + "; " if notes else "") + f"provider switch {previous_provider}->{provider}"

    return UniverseResult(provider, sorted(members), rows, diff, status, notes)
