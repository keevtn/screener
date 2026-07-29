"""Gate 1 task 1.3: EDGAR User-Agent resolution + client-side ≤10 req/s limiter.

ROADMAP-NOTE: the roadmap names respx for test_edgar_user_agent, but the EDGAR
poller is aiohttp (respx mocks httpx only). The UA is verified at the resolver and
the aiohttp header-build boundary; the rate limit by its deterministic timing.
"""

from __future__ import annotations

import asyncio

import pytest

from pipeline.ingest.edgar import PLACEHOLDER_UA, edgar_user_agent
from pipeline.ingest.ratelimit import RateLimiter

# --- UA resolution -----------------------------------------------------------


def test_edgar_user_agent_prefers_env(monkeypatch):
    monkeypatch.setenv("EDGAR_USER_AGENT", "MyOrg research contact@myorg.com")
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "ignored@example.com")
    assert edgar_user_agent() == "MyOrg research contact@myorg.com"


def test_edgar_user_agent_legacy_email_fallback(monkeypatch):
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "me@example.com")
    assert edgar_user_agent() == "FinancialNewsDashboard/1.0 (me@example.com)"


def test_edgar_user_agent_placeholder_when_unset(monkeypatch):
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    assert edgar_user_agent() == PLACEHOLDER_UA


def test_sec_extractor_sends_resolved_ua(monkeypatch):
    # The aiohttp session's default headers carry the UA on every EDGAR request.
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2] / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    monkeypatch.setenv("EDGAR_USER_AGENT", "MyOrg contact@myorg.com")
    from IngestionModule import _HttpClient

    assert _HttpClient._build_headers()["User-Agent"] == "MyOrg contact@myorg.com"


# --- rate limiter ------------------------------------------------------------


class FakeClock:
    """A monotonic clock that only advances when the fake sleep is awaited."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_rate_limiter_first_acquire_is_free():
    clock = FakeClock()
    rl = RateLimiter(10, now=clock.now, sleep=clock.sleep)
    assert asyncio.run(rl.acquire()) == 0.0
    assert clock.sleeps == []


def test_rate_limiter_enforces_min_spacing():
    clock = FakeClock()
    rl = RateLimiter(10.0, now=clock.now, sleep=clock.sleep)  # 0.1s min spacing

    async def drive():
        return [await rl.acquire() for _ in range(5)]

    waited = asyncio.run(drive())
    assert waited[0] == 0.0
    # Every subsequent back-to-back acquire waits exactly one interval (0.1s).
    assert all(w == pytest.approx(0.1) for w in waited[1:])
    # 5 requests spaced 0.1s apart -> never exceeds 10/s.
    assert clock.t == pytest.approx(0.4)


def test_rate_limiter_no_wait_when_spaced_out():
    clock = FakeClock()
    rl = RateLimiter(10.0, now=clock.now, sleep=clock.sleep)

    async def drive():
        first = await rl.acquire()
        clock.t += 1.0  # caller idles well beyond the interval
        second = await rl.acquire()
        return first, second

    first, second = asyncio.run(drive())
    assert first == 0.0 and second == 0.0  # spaced out -> no throttling


def test_rate_limiter_rejects_nonpositive():
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)
