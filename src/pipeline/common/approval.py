"""Human-gated config approval (docs/ROADMAP.md task 7.3, invariant I3).

The ONE path that turns an analyst proposal into a live config version. Both the
CLI (scripts/approve.py) and the API (POST /config/proposals/{id}/approve) call
these functions, so the two entry points share identical semantics: approval
applies the stored patch to the base params and calls get_or_create_config -> a
NEW immutable version; rejection archives the row with a reason. The approval
itself is the human gate — nothing here runs automatically.

Lives in `common` (not `agents`) on purpose: the agent layer only PROPOSES and
must never reference get_or_create_config (invariant I3, enforced by test).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from pipeline.agents.analyst import apply_patch
from pipeline.common.config import get_or_create_config
from pipeline.common.models import Config, PendingChange
from pipeline.common.timeutil import utcnow


class ApprovalError(ValueError):
    """A proposal cannot be approved/rejected as requested (missing / wrong state)."""


def approve_change(session: Session, change_id: str, *, notes: str = "") -> Config:
    """Apply a pending proposal's patch and mint the resulting immutable config.

    Raises ApprovalError if the proposal is missing, not pending, has an empty
    patch, or its base config is gone. On success the proposal is marked approved
    with resulting_config_version set, and the new (or content-identical existing)
    Config is returned.
    """
    change = session.get(PendingChange, change_id)
    if change is None:
        raise ApprovalError(f"no pending_change with id {change_id!r}")
    if change.status != "pending":
        raise ApprovalError(f"proposal {change_id} is {change.status}, not pending")
    if not change.patch_json:
        raise ApprovalError("proposal has an empty patch; nothing to apply (reject it instead)")
    base = session.get(Config, change.base_config_version)
    if base is None:
        raise ApprovalError(f"base config {change.base_config_version} not found")

    new_params = apply_patch(base.params_json, change.patch_json)
    cfg = get_or_create_config(session, new_params, notes=notes or f"approved change {change.id}")
    change.status = "approved"
    change.resolved_at = utcnow()
    change.resulting_config_version = cfg.config_version
    session.commit()
    return cfg


def reject_change(session: Session, change_id: str, *, reason: str) -> PendingChange:
    """Archive a pending proposal with a reason (no config version is created).

    Raises ApprovalError if the proposal is missing, not pending, or no reason
    was given.
    """
    change = session.get(PendingChange, change_id)
    if change is None:
        raise ApprovalError(f"no pending_change with id {change_id!r}")
    if change.status != "pending":
        raise ApprovalError(f"proposal {change_id} is {change.status}, not pending")
    if not reason or not reason.strip():
        raise ApprovalError("a rejection reason is required")

    change.status = "rejected"
    change.resolved_at = utcnow()
    change.resolved_reason = reason
    session.commit()
    return change
