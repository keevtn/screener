"""Earnings-calendar feed for the scheduled panel (docs/ROADMAP.md task 5b.1).

Finviz Elite export (primary — one bulk request, earnings-date column) with a
per-ticker yfinance fallback. Dates are treated as APPROXIMATE (reconciled against
actual filings later). Populates scheduled_events via upsert (idempotent).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
from dateutil import parser as dateutil_parser

log = logging.getLogger("pipeline.panel.earnings")

from pipeline.marketdata.finviz import FINVIZ_EXPORT_URL, FinvizAuthError
from pipeline.panel.scheduled import upsert_scheduled_event

# Earnings-time suffixes Finviz/vendors append (before/after market, a/b codes).
_SUFFIX_RE = re.compile(r"\b(BMO|AMC|before market|after market)\b|/[ab]\b", re.IGNORECASE)


def parse_earnings_date(cell: str | None, *, ref: date) -> date | None:
    """Parse a vendor earnings-date cell ('5/28/2026', 'May 28 AMC', 'Jul 24/a')."""
    if not cell:
        return None
    text = _SUFFIX_RE.sub("", cell).strip().strip("-").strip()
    if not text or text in ("N/A", "-"):
        return None
    try:
        dt = dateutil_parser.parse(text, default=datetime(ref.year, 1, 1, tzinfo=UTC))
    except (ValueError, OverflowError):
        return None
    d = dt.date()
    # A month-only date parsed as this year but already past likely means next year.
    if d < ref and (ref - d).days > 200:
        d = d.replace(year=d.year + 1)
    return d


@dataclass
class SnapshotStats:
    universe: int = 0
    finviz_hits: int = 0
    yfinance_hits: int = 0
    upserted: int = 0
    finviz_error: str | None = None


class FinvizEarningsProvider:
    """Bulk earnings dates from the Finviz Elite export (header-detected column)."""

    name = "finviz"

    def __init__(
        self,
        auth_token: str,
        *,
        client: httpx.Client | None = None,
        view: str = "152",
        filters: str = "geo_usa,ind_stocksonly",
        timeout: float = 30.0,
    ) -> None:
        if not auth_token:
            raise FinvizAuthError("FINVIZ_AUTH_TOKEN is empty (I9: set it in .env)")
        self._auth = auth_token
        self._client = client
        self._view = view
        self._filters = filters
        self._timeout = timeout

    def fetch_earnings(self, *, ref: date) -> dict[str, date]:
        # c=1,68 = Ticker + Earnings Date (custom export column set).
        params: dict[str, Any] = {
            "v": self._view,
            "f": self._filters,
            "c": "1,68",
            "auth": self._auth,
        }
        if self._client is not None:
            resp = self._client.get(FINVIZ_EXPORT_URL, params=params, timeout=self._timeout)
        else:
            resp = httpx.get(
                FINVIZ_EXPORT_URL, params=params, timeout=self._timeout, follow_redirects=True
            )
        resp.raise_for_status()
        return parse_finviz_earnings(resp.text, ref=ref)


def parse_finviz_earnings(text: str, *, ref: date) -> dict[str, date]:
    """Extract {ticker: earnings_date} from an export CSV (empty if no earnings col)."""
    stripped = text.lstrip()
    if stripped.startswith("<"):
        raise FinvizAuthError("Finviz export returned HTML, not CSV — auth token likely expired")
    if stripped.lower().startswith("invalid") and "token" in stripped.lower():
        raise FinvizAuthError(f"Finviz export rejected the auth token: {stripped[:80]!r}")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    earn_col = next((h for h in header if "earnings" in h.lower()), None)
    if earn_col is None:
        return {}  # this saved view has no earnings column -> caller falls back to yfinance
    out: dict[str, date] = {}
    for rec in reader:
        ticker = (rec.get("Ticker") or "").strip().upper()
        if not ticker:
            continue
        d = parse_earnings_date(rec.get(earn_col), ref=ref)
        if d is not None:
            out[ticker] = d
    return out


def next_earnings_yfinance(ticker: str, *, ref: date, timeout_s: float = 20.0) -> date | None:
    """Next future earnings date for a ticker via yfinance, or None.

    get_earnings_dates() takes no timeout kwarg and rides yfinance's own socket
    defaults, so it's run in a worker thread with a hard `timeout_s` — one hung
    ticker must not stall the per-universe snapshot loop (the same silent-stall
    class as the fixed yf.download). On timeout the thread is abandoned (rare;
    best-effort fallback data) and None returned."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as _FTimeout

    def _fetch():
        import yfinance as yf

        return yf.Ticker(ticker).get_earnings_dates(limit=12)

    ex = ThreadPoolExecutor(max_workers=1)
    try:
        df = ex.submit(_fetch).result(timeout=timeout_s)
    except _FTimeout:
        log.warning("yfinance earnings lookup timed out for %s (%.0fs)", ticker, timeout_s)
        return None
    except Exception:  # noqa: BLE001 — yfinance is best-effort/fallback
        return None
    finally:
        ex.shutdown(wait=False)  # never block the caller on a hung fetch
    if df is None or getattr(df, "empty", True):
        return None
    future = sorted(ts.date() for ts in df.index if ts.date() >= ref)
    return future[0] if future else None


def snapshot_earnings(
    session,
    universe: list[str],
    *,
    finviz_provider: FinvizEarningsProvider | None = None,
    yfinance_lookup=next_earnings_yfinance,
    now: datetime | None = None,
) -> SnapshotStats:
    """Upsert next-earnings scheduled events for the universe (Finviz -> yfinance)."""
    from pipeline.common.timeutil import utcnow

    ref_dt = now or utcnow()
    ref = ref_dt.date()
    stats = SnapshotStats(universe=len(universe))

    finviz_map: dict[str, date] = {}
    if finviz_provider is not None:
        try:
            finviz_map = finviz_provider.fetch_earnings(ref=ref)
        except Exception as exc:  # noqa: BLE001 — degrade to yfinance, but record why
            stats.finviz_error = f"{type(exc).__name__}: {exc}"[:160]

    for ticker in universe:
        d = finviz_map.get(ticker)
        source = "finviz"
        if d is not None:
            stats.finviz_hits += 1
        else:
            d = yfinance_lookup(ticker, ref=ref)
            source = "yfinance"
            if d is not None:
                stats.yfinance_hits += 1
        if d is None or d < ref:
            continue
        upsert_scheduled_event(
            session,
            ticker,
            "earnings_results",
            d,
            stage="scheduled",
            source=source,
            meta={"approximate": True},
            now=ref_dt,
        )
        stats.upserted += 1
    return stats
