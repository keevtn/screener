"""Async client-side rate limiter (docs/ROADMAP.md task 1.3).

Enforces a minimum spacing between acquisitions so a poller stays at or under a
target rate (EDGAR: ≤10 req/s). The clock and sleep are injectable so the timing
is tested deterministically without real waits.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    """At most ``max_per_sec`` acquisitions per second (min spacing 1/max_per_sec)."""

    def __init__(
        self,
        max_per_sec: float,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_per_sec <= 0:
            raise ValueError("max_per_sec must be positive")
        self.min_interval = 1.0 / max_per_sec
        self._now = now
        self._sleep = sleep
        self._next: float | None = None

    async def acquire(self) -> float:
        """Block until the next slot is due. Returns the seconds waited (0 if free)."""
        now = self._now()
        if self._next is None:
            self._next = now
        waited = 0.0
        if now < self._next:
            waited = self._next - now
            await self._sleep(waited)
        # Schedule the following slot one interval after the slot just consumed.
        self._next = max(self._next, now) + self.min_interval
        return waited
