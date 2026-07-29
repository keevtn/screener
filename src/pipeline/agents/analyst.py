"""Weekly analyst (docs/ROADMAP.md task 7.3).

Reads the graded ledger + metrics, asks the model for a markdown report and an
OPTIONAL config patch, and writes a `pending_changes` row. It NEVER creates a
config version (I3): the human runs scripts/approve.py, which applies the patch
and calls the versioned loader — the only path that mints a version.

Patch shape: a flat {"dotted.param.path": new_value} map. Only paths that already
exist in the base params are kept (the analyst cannot invent knobs).
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from pipeline.agents.client import (
    DEFAULT_ANALYST_MODEL,
    LLMClient,
    enforce_daily_cap,
    log_spend,
    resolve_model,
)
from pipeline.agents.schemas import AnalystOutput
from pipeline.common.models import PendingChange
from pipeline.common.timeutil import utcnow
from pipeline.grade.metrics import metrics_by_config

ANALYST_SYSTEM = """You are a quantitative research lead reviewing a news-prediction
system's weekly performance.

You receive: the current config parameters and per-config grading metrics (hit
rate, coverage, precision/recall, sample sizes).

Write a concise markdown report of what the ledger shows, then propose AT MOST ONE
parameter change if — and only if — the evidence supports it. Be conservative:
small samples rarely justify a change; "no change" is a valid and common answer.

Return ONLY a JSON object:
{"report_md": "...", "rationale": "...", "proposed_patch": {"dotted.path": value}}
- proposed_patch keys must be dotted paths into the given config params (e.g.
  "sentiment_threshold" or "tier_weights.1"); an empty object means no change.
- Never propose changes to the prediction contract (horizon_trading_days,
  threshold, benchmark_symbol). You propose; a human approves."""

# Contract constants the analyst must never touch (defense-in-depth; approve.py
# also refuses). Kept explicit so the guard is auditable.
FROZEN_PATHS = frozenset({"horizon_trading_days", "threshold", "benchmark_symbol"})


def _get_path(params: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Return (exists, value) for a dotted path into a nested dict."""
    cur: Any = params
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def validate_patch(params: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Keep only patch entries whose path already exists and is not frozen."""
    clean: dict[str, Any] = {}
    for path, value in patch.items():
        if path in FROZEN_PATHS or path.split(".")[0] in FROZEN_PATHS:
            continue
        exists, _ = _get_path(params, path)
        if exists:
            clean[path] = value
    return clean


def apply_patch(params: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of params with the (already-validated) patch applied."""
    out = copy.deepcopy(params)
    for path, value in patch.items():
        keys = path.split(".")
        cur = out
        for key in keys[:-1]:
            cur = cur[key]
        cur[keys[-1]] = value
    return out


def build_analyst_context(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Compact JSON context: current params + per-config grading metrics."""
    metrics = metrics_by_config(session)
    return {
        "config_params": params,
        "metrics_by_config": [
            {
                "config_version": m.config_version,
                "total_graded": m.total_graded,
                "correct": m.correct,
                "incorrect": m.incorrect,
                "expired": m.expired,
                "hit_rate": m.hit_rate,
                "coverage": m.coverage,
                "precision": m.precision,
                "recall": m.recall,
                "mean_lead_time_days": m.mean_lead_time_days,
            }
            for m in metrics.values()
        ],
    }


def parse_analyst_output(text: str) -> AnalystOutput:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    return AnalystOutput.model_validate(json.loads(raw))


def run_analyst(
    session: Session,
    client: LLMClient,
    *,
    base_config_version: str,
    params: dict[str, Any],
    model: str = DEFAULT_ANALYST_MODEL,
    now: datetime | None = None,
    cap: float | None = None,
    max_tokens: int = 4096,
) -> PendingChange | None:
    """Produce a pending_changes proposal from the graded ledger. None on failure."""
    now = now or utcnow()
    model = resolve_model(model)
    enforce_daily_cap(session, cap=cap, now=now)

    context = build_analyst_context(session, params)
    user = f"Weekly review context (JSON):\n{json.dumps(context, ensure_ascii=False)}"
    result = client.complete(system=ANALYST_SYSTEM, user=user, model=model, max_tokens=max_tokens)

    try:
        parsed = parse_analyst_output(result.text)
    except (ValueError, ValidationError):
        log_spend(session, result, purpose="analyst", ok=False, now=now)
        session.commit()
        return None

    log_spend(session, result, purpose="analyst", ok=True, now=now)
    clean_patch = validate_patch(params, parsed.proposed_patch)
    change = PendingChange(
        created_at=now,
        base_config_version=base_config_version,
        patch_json=clean_patch,
        rationale=parsed.rationale,
        report_md=parsed.report_md,
        status="pending",
    )
    session.add(change)
    session.commit()
    return change
