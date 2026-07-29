"""Nasdaq Trader symbol directory — universe fallback provider (task 0.6).

Fallback in the chain when Finviz is unavailable. Provides *listings* only (no
fundamentals): the pipe-delimited ``nasdaqlisted.txt`` / ``otherlisted.txt``
files. Test issues and ETFs are dropped so the candidate set is common-stock-ish.

ROADMAP-NOTE: without fundamentals, universe.yaml's numeric floors can't be
applied here. The materializer therefore restricts fallback membership to the
watchlist plus previously-approved members that are still listed (it can't newly
qualify a ticker), and stamps the snapshot ``symbol_directory`` so the provenance
is explicit. Richer fallback filtering (from cached bars + last fundamentals
snapshot) is a later refinement.
"""

from __future__ import annotations

import httpx

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


def parse_symbol_directory(text: str) -> list[str]:
    """Parse a Nasdaq Trader pipe-delimited directory file into a symbol list.

    Drops the header, the trailing 'File Creation Time' footer, ETFs, and test
    issues. Handles both nasdaqlisted (Symbol, ETF, Test Issue cols) and
    otherlisted (ACT Symbol, ... , ETF, Test Issue) layouts.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split("|")
    try:
        sym_idx = header.index("Symbol")
    except ValueError:
        sym_idx = header.index("ACT Symbol") if "ACT Symbol" in header else 0
    etf_idx = header.index("ETF") if "ETF" in header else None
    test_idx = header.index("Test Issue") if "Test Issue" in header else None

    out: list[str] = []
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        cols = line.split("|")
        if sym_idx >= len(cols):
            continue
        symbol = cols[sym_idx].strip().upper()
        if not symbol:
            continue
        if etf_idx is not None and etf_idx < len(cols) and cols[etf_idx].strip() == "Y":
            continue
        if test_idx is not None and test_idx < len(cols) and cols[test_idx].strip() == "Y":
            continue
        out.append(symbol)
    return out


class SymbolDirectoryProvider:
    name = "symbol_directory"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = client
        self._timeout = timeout

    def _get(self, url: str) -> str:
        if self._client is not None:
            resp = self._client.get(url, timeout=self._timeout)
        else:
            resp = httpx.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.text

    def fetch_symbols(self) -> list[str]:
        symbols: set[str] = set()
        for url in (NASDAQ_LISTED_URL, OTHER_LISTED_URL):
            symbols.update(parse_symbol_directory(self._get(url)))
        return sorted(symbols)
