"""
listed_symbols.py
=================
Authoritative universe of **every US-listed ticker symbol — equities AND ETFs** —
from the official Nasdaq Trader symbol directory.

Why a second source (we already have SEC's company_tickers.json)
---------------------------------------------------------------
SEC's ``company_tickers.json`` lists operating companies that file 10-Ks, so it
covers only *some* ETFs (SPY is in it; SOXL, IWM, SMH, XLF, TLT are not). ETFs are
among the most-discussed symbols on social feeds, so validating cashtags against
the SEC file alone wrongly strips real ETFs. The Nasdaq Trader directory is the
canonical list of all listed securities across every US exchange (NASDAQ, NYSE,
NYSE Arca, Cboe BZX …), ETFs included, and is what the exchanges themselves
publish. Unioned with the SEC file and the crypto allowlist it gives a complete
"is this a real, tradable ticker?" set.

  nasdaqlisted.txt — NASDAQ-listed securities. Columns:
    Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
  otherlisted.txt  — everything else (NYSE, NYSE Arca, Cboe …). Columns:
    ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol

Both files carry a header row and a trailing ``File Creation Time`` line, and mark
test/placeholder issues with a ``Test Issue`` flag — all filtered out here.

Pure parsing (``parse_symbol_directory``) is unit-tested; ``load_listed_symbols``
adds the cached network fetch and never raises — a fetch failure yields the last
good set (or an empty one), so validation degrades to un-gated rather than
dropping every ticker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

log = logging.getLogger("listed_symbols")

_NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
_OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
_HEADERS = {
    "User-Agent": "FinancialNewsDashboard/1.0 (research/non-commercial)",
    "Accept": "text/plain",
}
_TTL_SECONDS = 24 * 3600.0  # the directory changes slowly; refresh daily
_TIMEOUT = 25

# Header labels that must never be treated as a symbol.
_HEADER_TOKENS = frozenset({"SYMBOL", "ACT SYMBOL", "CQS SYMBOL", "NASDAQ SYMBOL"})

_cache: dict[str, Any] = {"fetched_at": 0.0, "symbols": set()}
_lock = asyncio.Lock()


def parse_symbol_directory(
    text: str, *, symbol_col: int = 0, test_issue_col: Optional[int] = None
) -> set[str]:
    """
    Parse a pipe-delimited Nasdaq Trader directory file into a set of uppercase
    ticker symbols. Skips the header row, the trailing ``File Creation Time`` line,
    blank lines, and (when ``test_issue_col`` is given) rows flagged as test issues.
    """
    out: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("File Creation Time"):
            continue
        parts = line.split("|")
        if symbol_col >= len(parts):
            continue
        sym = parts[symbol_col].strip().upper()
        if not sym or sym in _HEADER_TOKENS:
            continue
        if test_issue_col is not None and test_issue_col < len(parts):
            if parts[test_issue_col].strip().upper() == "Y":
                continue
        out.add(sym)
    return out


async def _fetch_text(url: str, session: Any) -> Optional[str]:
    """GET a directory file as text; None on any failure (never raises)."""
    try:
        async with session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("%s HTTP %s", url.rsplit("/", 1)[-1], resp.status)
                return None
            return await resp.text()
    except Exception as exc:  # noqa: BLE001
        log.warning("%s fetch failed: %s", url.rsplit("/", 1)[-1], type(exc).__name__)
        return None


async def load_listed_symbols(
    *, session: Any = None, force: bool = False
) -> set[str]:
    """
    Cached set of all US-listed symbols (both directory files). Fetches at most
    once per ``_TTL_SECONDS``; on failure returns the last good set (possibly
    empty). Safe to call on every ingestion start.
    """
    now = time.monotonic()
    if not force and _cache["symbols"] and (now - _cache["fetched_at"]) < _TTL_SECONDS:
        return _cache["symbols"]

    async with _lock:
        now = time.monotonic()
        if not force and _cache["symbols"] and (now - _cache["fetched_at"]) < _TTL_SECONDS:
            return _cache["symbols"]

        try:
            import aiohttp
        except ImportError:
            return _cache["symbols"]

        owns = session is None
        if owns:
            session = aiohttp.ClientSession(headers=_HEADERS)
        try:
            nasdaq_txt = await _fetch_text(_NASDAQ_URL, session)
            other_txt = await _fetch_text(_OTHER_URL, session)
        finally:
            if owns:
                await session.close()

        symbols: set[str] = set()
        if nasdaq_txt:
            # nasdaqlisted: Symbol=col0, Test Issue=col3
            symbols |= parse_symbol_directory(nasdaq_txt, symbol_col=0, test_issue_col=3)
        if other_txt:
            # otherlisted: ACT Symbol=col0, Test Issue=col6
            symbols |= parse_symbol_directory(other_txt, symbol_col=0, test_issue_col=6)

        if symbols:
            _cache["symbols"] = symbols
            _cache["fetched_at"] = time.monotonic()
            log.info("loaded %d US-listed symbols", len(symbols))
        return _cache["symbols"]


async def load_valid_tickers(*, session: Any = None, force: bool = False) -> set[str]:
    """
    The full real-ticker validation universe used to gate social cashtags:
    all US-listed symbols (Nasdaq directory) ∪ SEC's company_tickers.json ∪ the
    major-crypto and market-index allowlists. Returns an empty set only if *every*
    listed source failed — callers treat empty as "no universe / do not gate" so a
    transient outage never strips all tickers.
    """
    from ticker_extractor import CRYPTO_TICKERS, INDEX_TICKERS

    listed = await load_listed_symbols(session=session, force=force)

    sec: set[str] = set()
    try:
        from edgar_tickers import load_company_names
        sec = set(await load_company_names(session=session, force=force))
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC name map unavailable (%s)", type(exc).__name__)

    if not listed and not sec:
        return set()  # fail open — no universe
    return listed | sec | set(CRYPTO_TICKERS) | set(INDEX_TICKERS)
