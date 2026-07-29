"""Finviz Elite universe/fundamentals provider (docs/ROADMAP.md task 0.6).

Primary provider in the universe chain: authenticated Elite CSV export
(``FINVIZ_AUTH_TOKEN`` from env, I9). Uses only ``httpx`` + stdlib ``csv`` — no
new runtime packages. Real-time quotes are out of scope; daily bars come from
the 0.4 provider, unchanged.

Two failure modes are detected explicitly rather than silently mis-parsed:
- **Auth expiry** — an expired/invalid token yields Finviz's HTML login page
  instead of CSV; recognized by leading '<', raised as FinvizAuthError.
- **Schema drift** — a required column missing/renamed in the export header is
  raised as FinvizSchemaError (the Phase 8 sentinel's guardrail, encoded early).

ROADMAP-NOTE: the exact Elite export column headers and request params depend on
the account's saved view; REQUIRED_COLUMNS is the contract this code enforces and
must be reconciled against the live export. The schema-drift check is precisely
what surfaces a mismatch in production instead of writing garbage fundamentals.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any

import httpx

FINVIZ_EXPORT_URL = "https://elite.finviz.com/export.ashx"

# Columns the parser requires (and maps to FundamentalsRow). A missing one is
# schema drift, not a silently-null field.
REQUIRED_COLUMNS = (
    "Ticker",
    "Sector",
    "Industry",
    "Country",
    "Market Cap",
    "Price",
    "Average Volume",
    "Shares Float",
    "Short Float",
    "Insider Ownership",
    "Institutional Ownership",
    "Beta",
)

_SUFFIX_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


class FinvizAuthError(RuntimeError):
    """The export returned HTML (login page) — the auth token is expired/invalid."""


class FinvizSchemaError(RuntimeError):
    """The export header is missing a required column (schema drift)."""


@dataclass(frozen=True)
class FundamentalsRow:
    ticker: str
    market_cap: float | None = None
    shares_float: float | None = None
    short_float: float | None = None
    insider_own: float | None = None
    inst_own: float | None = None
    avg_volume: float | None = None
    beta: float | None = None
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    price: float | None = None
    change_pct: float | None = None  # from the "Change" column (optional)


def parse_finviz_number(value: str | None) -> float | None:
    """Parse a Finviz cell: '2.34B', '1,234,567', '5.23%', '-' (missing) -> float."""
    if value is None:
        return None
    text = value.strip().replace(",", "")
    if text in ("", "-", "N/A"):
        return None
    pct = text.endswith("%")
    if pct:
        text = text[:-1]
    mult = 1.0
    if text and text[-1].upper() in _SUFFIX_MULT:
        mult = _SUFFIX_MULT[text[-1].upper()]
        text = text[:-1]
    try:
        num = float(text) * mult
    except ValueError:
        return None
    return num / 100.0 if pct else num


def parse_finviz_csv(text: str) -> list[FundamentalsRow]:
    """Parse an Elite export CSV body into FundamentalsRow list.

    Raises FinvizAuthError if the body is HTML (auth expiry) and FinvizSchemaError
    if a required column is absent.
    """
    if text.lstrip().startswith("<"):
        raise FinvizAuthError("Finviz export returned HTML, not CSV — auth token likely expired")

    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise FinvizSchemaError(f"Finviz export missing required columns: {missing}")

    rows: list[FundamentalsRow] = []
    for rec in reader:
        ticker = (rec.get("Ticker") or "").strip().upper()
        if not ticker:
            continue
        rows.append(
            FundamentalsRow(
                ticker=ticker,
                market_cap=parse_finviz_number(rec.get("Market Cap")),
                shares_float=parse_finviz_number(rec.get("Shares Float")),
                short_float=parse_finviz_number(rec.get("Short Float")),
                insider_own=parse_finviz_number(rec.get("Insider Ownership")),
                inst_own=parse_finviz_number(rec.get("Institutional Ownership")),
                avg_volume=parse_finviz_number(rec.get("Average Volume")),
                beta=parse_finviz_number(rec.get("Beta")),
                sector=(rec.get("Sector") or "").strip() or None,
                industry=(rec.get("Industry") or "").strip() or None,
                country=(rec.get("Country") or "").strip() or None,
                price=parse_finviz_number(rec.get("Price")),
                change_pct=parse_finviz_number(rec.get("Change")),  # optional column
            )
        )
    return rows


class FinvizProvider:
    """Primary universe/fundamentals provider (Finviz Elite CSV export)."""

    name = "finviz"

    # Explicit export column ids so the pull does not depend on the account's saved
    # view: Ticker, Sector, Industry, Country, Market Cap, Shares Float, Insider Own,
    # Inst Own, Short Float, Beta, Avg Volume, Price, Change, Volume.
    DEFAULT_COLUMNS = "1,3,4,5,6,25,26,28,30,48,63,65,66,67"

    def __init__(
        self,
        auth_token: str,
        *,
        client: httpx.Client | None = None,
        filters: str = "geo_usa,ind_stocksonly",
        view: str = "152",
        columns: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not auth_token:
            raise FinvizAuthError("FINVIZ_AUTH_TOKEN is empty (I9: set it in .env)")
        self._auth = auth_token
        self._client = client
        self._filters = filters
        self._view = view
        self._columns = columns or self.DEFAULT_COLUMNS
        self._timeout = timeout

    def fetch_fundamentals(self) -> list[FundamentalsRow]:
        params: dict[str, Any] = {
            "v": self._view,
            "f": self._filters,
            "c": self._columns,
            "auth": self._auth,
        }
        if self._client is not None:
            resp = self._client.get(FINVIZ_EXPORT_URL, params=params, timeout=self._timeout)
        else:
            # Finviz 301-redirects export.ashx -> /export; follow it (like the
            # earnings provider) so the bulk pull doesn't fail on the redirect.
            resp = httpx.get(
                FINVIZ_EXPORT_URL, params=params, timeout=self._timeout, follow_redirects=True
            )
        resp.raise_for_status()
        return parse_finviz_csv(resp.text)
