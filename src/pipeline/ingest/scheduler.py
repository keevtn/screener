"""APScheduler wiring for per-source polling (docs/ROADMAP.md task 1.2).

Pure scheduler config: it knows per-source cadences and how to register jobs, but
nothing about the extractors. The dispatch function (which drives the backend
pollers into the raw_items sink) is injected, so the manual path
(scripts/dispatch.py) and the scheduled path call the exact same code — that is
what makes ``test_dispatch_equals_scheduler`` identical by construction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Per-source poll cadence in seconds (roadmap 1.2): EDGAR 1–5 min, RSS 5–15 min,
# social per API limits. Conservative defaults; tighten around scheduled events
# later (Phase 5b).
DEFAULT_CADENCES: dict[str, int] = {
    "sec": 300,
    "rss": 600,
    "fda": 300,
    "reddit": 180,
}

DispatchFn = Callable[[str], Awaitable[int]]


def build_scheduler(
    dispatch_fn: DispatchFn,
    *,
    cadences: dict[str, int] | None = None,
    scheduler: AsyncIOScheduler | None = None,
) -> tuple[AsyncIOScheduler, dict[str, object]]:
    """Register one interval job per source, all calling ``dispatch_fn(source)``.

    Returns the scheduler plus a ``{source: Job}`` map so callers/tests can drive
    an individual job without starting the loop.
    """
    cadences = cadences or DEFAULT_CADENCES
    sched = scheduler or AsyncIOScheduler()
    jobs: dict[str, object] = {}
    for source, seconds in cadences.items():
        jobs[source] = sched.add_job(
            dispatch_fn,
            IntervalTrigger(seconds=seconds),
            args=[source],
            id=f"poll:{source}",
            replace_existing=True,
        )
    return sched, jobs
