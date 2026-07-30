"""Read-ONLY Alpaca paper-account reader — the TRADER dashboard's data source.

The web app is strictly read-only toward Alpaca: it may look at the paper book
(account, positions, orders, activities, portfolio history, clock, calendar) but
it must NEVER place, cancel, or modify an order. That write path lives with the
local sim driver + human gate (``pipeline.sim.broker``) and is deliberately not
importable from here.

This class enforces that structurally, not just by convention:

  * Paper endpoint ONLY. Constructing it against anything but
    ``paper-api.alpaca.markets`` raises, regardless of env values — the same hard
    guardrail the broker uses. There is no code path from here to a live account.
  * There are NO ``post``/``delete``/``submit`` methods. The class exposes ``_get``
    and nothing else on the wire, so no caller (or future edit that stays within
    the class's vocabulary) can express an order. Order placement is impossible
    here by construction.
  * A short TTL cache fronts every GET so many dashboard viewers polling at ~10s
    collapse to at most one upstream request per endpoint per ``ttl`` window —
    keys never multiply into rate-limit territory.

Degrade-graceful like the rest of the stack: no keys in the env ->
``paper_reader()`` returns None and callers render a "connect Alpaca keys" empty
state rather than crashing. A fresh paper account (no history yet) returns empty
lists / zeroed history, which the UI renders as an honest empty state.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pipeline.marketdata.alpaca import DATA_URL, PAPER_URL, alpaca_keys

log = logging.getLogger("pipeline.marketdata.paper_account")

# Default freshness window for the response cache. ~10s keeps the dashboard live
# while ensuring N viewers don't become N upstream calls.
DEFAULT_TTL_S = 10.0


class PaperAccountReaderError(RuntimeError):
    """A hard guardrail refused to construct the reader."""


class _TTLCache:
    """Tiny monotonic-clock TTL cache: key -> (expires_at, value). Not shared
    across processes (the API is single-instance by design); good enough to fold
    concurrent viewers into one upstream call per window."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> tuple[bool, Any]:
        hit = self._store.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return True, hit[1]
        return False, None

    def put(self, key: str, value: Any, ttl: float) -> None:
        self._store[key] = (time.monotonic() + ttl, value)


class PaperAccountReader:
    """GET-only REST client for the Alpaca *paper* trading + data APIs.

    ``http`` is injectable for tests (anything with ``.get(url, headers=, params=,
    timeout=)`` returning a requests-style response). ``ttl`` is the cache window
    in seconds (0 disables caching, e.g. in tests that want each call to hit).
    """

    def __init__(
        self,
        http: Any | None = None,
        *,
        base_url: str = PAPER_URL,
        data_url: str = DATA_URL,
        ttl: float = DEFAULT_TTL_S,
    ) -> None:
        # GUARDRAIL: paper endpoint only. No env value, no argument, nothing can
        # point this reader at a live account. Mirrors AlpacaPaperBroker.
        if base_url != PAPER_URL:
            raise PaperAccountReaderError(
                f"refusing to construct paper reader against non-paper endpoint {base_url!r}; "
                f"only {PAPER_URL} is permitted"
            )
        keys = alpaca_keys()
        if keys is None:
            raise PaperAccountReaderError(
                "Alpaca keys missing (ALPACA_API_KEY / ALPACA_API_SECRET)"
            )
        self._base = base_url
        self._data = data_url
        self._headers = {"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]}
        import requests

        self._http = http or requests.Session()
        self._ttl = float(ttl)
        self._cache = _TTLCache()

    # --- the ONLY wire primitive: read. No post/delete exists on this class. ---
    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        cache_key = f"{url}?{sorted((params or {}).items())}"
        if self._ttl > 0:
            hit, val = self._cache.get(cache_key)
            if hit:
                return val
        r = self._http.get(url, headers=self._headers, params=params or {}, timeout=15)
        r.raise_for_status()
        body = r.json()
        if self._ttl > 0:
            self._cache.put(cache_key, body, self._ttl)
        return body

    # --- account + clock ---------------------------------------------------
    def account(self) -> dict[str, Any]:
        """Raw paper account snapshot. ``endpoint`` is echoed so the caller can
        prove it read the paper host."""
        acct = self._get(f"{self._base}/v2/account")
        return {**acct, "endpoint": self._base}

    def clock(self) -> dict[str, Any]:
        """Alpaca trading clock: {is_open, timestamp, next_open, next_close}
        (raw ISO strings — the API returns ET-offset ISO that natively encodes
        holidays/half-days/DST). Caller parses as needed."""
        return self._get(f"{self._base}/v2/clock")

    # --- positions ---------------------------------------------------------
    def positions(self) -> list[dict[str, Any]]:
        """All open positions (empty list on a fresh account)."""
        return self._get(f"{self._base}/v2/positions") or []

    # --- orders ------------------------------------------------------------
    def orders(
        self,
        *,
        status: str = "all",
        limit: int = 500,
        after: str | None = None,
        until: str | None = None,
        direction: str = "desc",
        nested: bool = True,
    ) -> list[dict[str, Any]]:
        """Orders for the account. ``status`` is open|closed|all. ``nested`` folds
        multi-leg orders under their parent. Empty list on a fresh account."""
        params: dict[str, Any] = {
            "status": status,
            "limit": max(1, min(int(limit), 500)),
            "direction": direction,
            "nested": str(bool(nested)).lower(),
        }
        if after:
            params["after"] = after
        if until:
            params["until"] = until
        return self._get(f"{self._base}/v2/orders", params) or []

    # --- account activities (fills etc.) -----------------------------------
    def activities(
        self, *, activity_types: str | None = "FILL", page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Account activities (defaults to FILL — the trade prints). Empty on a
        fresh account."""
        params: dict[str, Any] = {"page_size": max(1, min(int(page_size), 100))}
        if activity_types:
            params["activity_types"] = activity_types
        return self._get(f"{self._base}/v2/account/activities", params) or []

    # --- portfolio equity curve --------------------------------------------
    def portfolio_history(
        self,
        *,
        period: str = "1M",
        timeframe: str = "1D",
        extended_hours: bool = True,
    ) -> dict[str, Any]:
        """The equity curve: parallel arrays {timestamp[], equity[], profit_loss[],
        profit_loss_pct[], base_value, timeframe}. A fresh account returns short/
        flat arrays, which the UI renders as an honest 'no history yet' state."""
        params: dict[str, Any] = {
            "period": period,
            "timeframe": timeframe,
            "extended_hours": str(bool(extended_hours)).lower(),
        }
        return self._get(f"{self._base}/v2/account/portfolio/history", params) or {}

    # --- market calendar ---------------------------------------------------
    def calendar(self, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        """Trading calendar rows {date, open, close, session_open, session_close}
        for [start, end]. Used by the Phase 2 P&L calendar to know which days were
        sessions."""
        params: dict[str, Any] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        return self._get(f"{self._base}/v2/calendar", params) or []


def paper_reader(http: Any | None = None, *, ttl: float = DEFAULT_TTL_S) -> PaperAccountReader | None:
    """Construct a reader, or None when Alpaca keys are absent. The single entry
    point the API uses so every endpoint degrades to a "connect Alpaca keys" empty
    state identically rather than raising."""
    if alpaca_keys() is None:
        return None
    try:
        return PaperAccountReader(http, ttl=ttl)
    except PaperAccountReaderError:
        return None
