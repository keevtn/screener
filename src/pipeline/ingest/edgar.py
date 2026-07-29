"""EDGAR fair-access helpers (docs/ROADMAP.md task 1.3).

SEC asks every automated client to declare a User-Agent identifying the operator
with a real contact, and to stay under ~10 requests/second. This is the canonical
User-Agent resolver; the rate limit lives in ``ratelimit.RateLimiter``.

ROADMAP-NOTE: the roadmap's `test_edgar_user_agent` calls for a respx assertion,
but the EDGAR poller is aiohttp (respx mocks httpx only). The UA is instead
verified at the resolver + header-build boundary, and the limiter by its timing.
"""

from __future__ import annotations

import os

# Clearly-placeholder default so an unconfigured deployment is obvious in logs
# (and SEC throttles it) rather than silently impersonating someone.
PLACEHOLDER_UA = "Market-News-Pipeline set-EDGAR_USER_AGENT@example.com"


def edgar_user_agent() -> str:
    """Resolve the EDGAR User-Agent, read at call time (not import time).

    Priority: ``EDGAR_USER_AGENT`` (the roadmap's declared full UA) →
    legacy ``SEC_CONTACT_EMAIL`` formatted → placeholder.
    """
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if ua:
        return ua
    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if email:
        return f"FinancialNewsDashboard/1.0 ({email})"
    return PLACEHOLDER_UA
