"""Exit policies — exits as a first-class, versioned, improvable model layer.

The sim's exit was hard-coded (hold to the horizon / intraday cutoff). This makes
the EXIT a pluggable policy the same way the entry is a config: a small frozen
spec, content-addressed (I3 discipline), that the engine consults each sweep and
the lab replays over captured minute paths. The point is NOT a magic take-profit
number — it is a framework to *strategize* exits and let exit ideas earn adoption
via evidence, exactly like entry hypotheses.

DEFAULT IS NO CHANGE. A config with no ``exit_policy`` behaves exactly as today:
horizon-hold (exit when the trading-day horizon elapses, or the intraday cutoff
for horizon-0). Nothing goes live on a real config without an explicit assignment.

Policy kinds (spec = a small JSON dict, ``{"kind": ..., <params>}``):
  * ``horizon_hold``            — baseline; exit at the horizon / intraday cutoff.
  * ``stop``                    — fixed adverse stop (``stop`` frac), horizon-backstopped.
  * ``bracket``                 — ``stop`` + ``take`` take-profit, horizon-backstopped.
  * ``trailing_after_threshold``— arm at ``arm`` favorable, exit on ``trail`` retrace from peak.
  * ``time_decay``              — cut the hold to ``max_hold_hours`` (< horizon).
  * ``vol_stop``                — adverse stop = ``atr_mult`` × recent-vol (ATR-scaled).

Two evaluation surfaces, ONE policy spec:
  * :func:`decide_exit` — LIVE, per-sweep, from the current quote (+ optional
    peak/atr context). Stateless kinds run live now; trailing/vol_stop need
    peak/atr context and otherwise fall back to the horizon (documented) until
    the driver plumbs that state — the LAB implements them fully today.
  * :func:`replay_exit` — LAB, walks a full captured minute path (peak/vol from
    the path), so every kind is fully evaluatable now.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_POLICY: dict[str, Any] = {"kind": "horizon_hold"}
KINDS = frozenset(
    {"horizon_hold", "stop", "bracket", "trailing_after_threshold", "time_decay", "vol_stop"}
)


@dataclass(frozen=True)
class ExitDecision:
    exit: bool
    reason: str  # horizon | intraday_cutoff | stop | take | trailing | time_decay | vol_stop | hold


def resolve_exit_policy(params: dict[str, Any], catalyst_type: str | None) -> dict[str, Any]:
    """The exit policy for a trade, frozen with its config. Precedence:
    per-catalyst-type map (``exit_policy_by_catalyst``) → single ``exit_policy`` →
    the horizon-hold baseline. Lets FDA binaries, offerings and M&A carry
    different exit shapes under one config."""
    by_ct = params.get("exit_policy_by_catalyst")
    if isinstance(by_ct, dict) and catalyst_type and catalyst_type in by_ct:
        return by_ct[catalyst_type] or DEFAULT_POLICY
    return params.get("exit_policy") or DEFAULT_POLICY


def exit_policy_ref(policy: dict[str, Any] | None) -> str:
    """Content-addressed id (I3): identical policy spec → identical ``xp-<hash12>``.
    Lets a config variant that differs ONLY in exit policy be identified and raced."""
    blob = json.dumps(policy or DEFAULT_POLICY, sort_keys=True, separators=(",", ":"))
    return "xp-" + hashlib.sha256(blob.encode()).hexdigest()[:12]


def _bar_time(v: Any) -> datetime | None:
    """Parse a bar's timestamp (ISO string or datetime) to aware datetime, or None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _excursions(direction: int, entry: float, high: float, low: float) -> tuple[float, float]:
    """(max_adverse, max_favorable) excursions for the bar, in position terms
    (fractions). A long's adverse extreme is the low; a short's is the high."""
    if direction == 1:
        return (low / entry - 1.0, high / entry - 1.0)
    return (-(high / entry - 1.0), -(low / entry - 1.0))


# --- LIVE per-sweep decision ------------------------------------------------
def decide_exit(policy: dict[str, Any], ctx: dict[str, Any]) -> ExitDecision:
    """Should this open trade exit THIS sweep? ``ctx`` keys:
    entry, price (current quote), direction, horizon_reached (bool from the
    engine's horizon/cutoff logic), horizon_reason ('horizon'|'intraday_cutoff'),
    held_hours (float), and optionally peak_favorable / atr_frac for stateful kinds.

    The horizon is always a MAX-HOLD backstop: every policy still exits when
    ``horizon_reached`` even if its own trigger never fires. So the worst case of
    any policy is the current behavior — never a longer hold than today."""
    kind = policy.get("kind", "horizon_hold")
    horizon_reached = bool(ctx.get("horizon_reached"))
    horizon_reason = ctx.get("horizon_reason", "horizon")

    def _backstop() -> ExitDecision:
        return ExitDecision(horizon_reached, horizon_reason if horizon_reached else "hold")

    if kind == "horizon_hold" or kind not in KINDS:
        return _backstop()

    entry = ctx["entry"]
    fav = ctx["direction"] * (ctx["price"] / entry - 1.0)  # current favorable excursion

    if kind == "stop":
        return ExitDecision(True, "stop") if fav <= -policy["stop"] else _backstop()
    if kind == "bracket":
        if fav <= -policy["stop"]:
            return ExitDecision(True, "stop")
        if fav >= policy["take"]:
            return ExitDecision(True, "take")
        return _backstop()
    if kind == "time_decay":
        max_h = float(policy.get("max_hold_hours", 0.0))
        if max_h > 0 and float(ctx.get("held_hours", 0.0)) >= max_h:
            return ExitDecision(True, "time_decay")
        return _backstop()
    if kind == "trailing_after_threshold":
        peak = ctx.get("peak_favorable")
        if peak is not None and peak >= policy.get("arm", 0.0) and (peak - fav) >= policy["trail"]:
            return ExitDecision(True, "trailing")
        return _backstop()  # no peak state live yet -> horizon backstop (lab evaluates fully)
    if kind == "vol_stop":
        atr = ctx.get("atr_frac")
        if atr is not None and fav <= -policy.get("atr_mult", 2.0) * atr:
            return ExitDecision(True, "vol_stop")
        return _backstop()
    return _backstop()


# --- LAB path replay --------------------------------------------------------
def replay_exit(
    policy: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    entry: float,
    direction: int,
    baseline_exit: float,
    atr_frac: float | None = None,
    entered_at: datetime | None = None,
) -> tuple[float, str]:
    """Apply ``policy`` to a full minute path (oldest-first bars with high/low[/close/time]),
    first-trigger wins. Returns (exit_price, reason). No trigger → the trade's real
    baseline exit ('horizon'). Stop is assumed to fill first on a bar that touches
    both a stop and a take (conservative). ``time_decay`` needs bar ``time`` +
    ``entered_at`` (exits at that bar's close). Fully implements every kind."""
    kind = policy.get("kind", "horizon_hold")
    if kind == "horizon_hold" or kind not in KINDS:
        return baseline_exit, "horizon"

    stop = policy.get("stop")
    if kind == "vol_stop" and atr_frac is not None:
        stop = policy.get("atr_mult", 2.0) * atr_frac
    take = policy.get("take") if kind == "bracket" else None
    arm = policy.get("arm", 0.0)
    trail = policy.get("trail")
    max_hold_h = float(policy["max_hold_hours"]) if kind == "time_decay" else None
    peak = 0.0

    for b in bars:
        if max_hold_h is not None and entered_at is not None:
            bt = _bar_time(b.get("time"))
            if bt is not None and (bt - entered_at).total_seconds() / 3600.0 >= max_hold_h:
                close = b.get("close")
                px = float(close) if close is not None else entry * (1 + direction * 0.0)
                return px, "time_decay"
        adverse, favorable = _excursions(direction, entry, b["high"], b["low"])
        peak = max(peak, favorable)
        hit_stop = stop is not None and kind in ("stop", "bracket", "vol_stop") and adverse <= -stop
        hit_take = take is not None and favorable >= take
        if hit_stop and hit_take:
            return entry * (1 + direction * -stop), "stop"  # conservative: stop first
        if hit_stop:
            return entry * (1 + direction * -stop), ("vol_stop" if kind == "vol_stop" else "stop")
        if hit_take:
            return entry * (1 + direction * take), "take"
        if (
            kind == "trailing_after_threshold"
            and trail is not None
            and peak >= arm
            and (peak - favorable) >= trail
        ):
            return entry * (1 + direction * (peak - trail)), "trailing"
    return baseline_exit, "horizon"
