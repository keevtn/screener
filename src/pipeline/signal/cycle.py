"""Signal cycle + alert stub (docs/ROADMAP.md task 4.4).

After a poll/enrich/score cycle, evaluate tickers with fresh clusters, emit
predictions, resolve armed drift, and alert on each new prediction (console +
optional webhook). This is the hook the scheduler calls each cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from pipeline.common.models import Prediction
from pipeline.common.timeutil import utcnow
from pipeline.signal.armed import resolve_all_armed
from pipeline.signal.engine import SignalEngine

log = logging.getLogger("pipeline.signal.cycle")

Alert = Callable[[Prediction], None]


def console_alert(pred: Prediction) -> None:
    log.info(
        "[SIGNAL] %s %s conf=%.2f cfg=%s evidence=%s",
        pred.ticker,
        pred.direction,
        pred.confidence,
        pred.config_version,
        pred.evidence_json,
    )


def webhook_alert(url: str) -> Alert:
    """Best-effort POST of each prediction to a webhook (never raises)."""

    def _alert(pred: Prediction) -> None:
        try:
            import httpx

            httpx.post(
                url,
                json={
                    "ticker": pred.ticker,
                    "direction": pred.direction,
                    "confidence": pred.confidence,
                    "config_version": pred.config_version,
                    "issued_at": pred.issued_at.isoformat(),
                    "evidence": pred.evidence_json,
                },
                timeout=5.0,
            )
        except Exception as exc:  # noqa: BLE001 — alerts must never break the cycle
            log.warning("webhook alert failed: %s", exc)

    return _alert


def run_signal_cycle(
    session: Session,
    params: dict[str, Any],
    config_version: str,
    *,
    provider: Any = None,
    now: datetime | None = None,
    alert: Alert | None = None,
) -> list[Prediction]:
    """One evaluation cycle: structured signals + armed-drift resolutions -> alerts."""
    now = now or utcnow()
    alert = alert or console_alert
    engine = SignalEngine(session, params, config_version, now=now)
    preds = engine.evaluate_all()
    if provider is not None:
        preds += resolve_all_armed(session, provider, params, config_version, now=now)
    for pred in preds:
        alert(pred)
    return preds
