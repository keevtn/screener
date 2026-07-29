"""Entity backbone construction (docs/ROADMAP.md task 0.3).

The *backbone* is the full CIK<->ticker seed from SEC ``company_tickers.json`` —
loaded regardless of tradeable-universe membership (universe filtering is task
0.6, and only toggles the ``active`` flag). This module is pure: parsing and
alias construction with no network. The network fetch + DB write live in
``scripts/seed_entities.py``.

Dual-class rule (I-adjacent, roadmap 0.3): one canonical ticker per company.
SEC orders ``company_tickers.json`` with the primary/most-liquid listing first,
so on a repeated CIK the first row wins the canonical slot and later rows
(GOOG behind GOOGL, BRK-A behind BRK-B) fold in as alternate-class aliases.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

# Trailing corporate designators stripped to derive a short mechanical alias.
# Order matters only for the regex alternation; all are matched case-insensitively.
_SUFFIXES = [
    "incorporated",
    "inc",
    "corporation",
    "corp",
    "company",
    "co",
    "limited",
    "ltd",
    "l.l.c",
    "llc",
    "l.p",
    "lp",
    "plc",
    "n.v",
    "s.a",
    "ag",
    "holdings",
    "holding",
    "group",
    "class a",
    "class b",
    "class c",
]
_SUFFIX_RE = re.compile(
    r"[\s,]+(?:" + "|".join(re.escape(s) for s in _SUFFIXES) + r")\.?$",
    re.IGNORECASE,
)
_THE_PREFIX_RE = re.compile(r"^the\s+", re.IGNORECASE)


def mechanical_alias(name: str) -> str:
    """Short alias for a company: drop a leading 'The' and trailing designators.

    Applied repeatedly so 'Foo Holdings Inc.' -> 'Foo'. Returns the input
    unchanged (trimmed) if nothing strips.
    """
    out = _THE_PREFIX_RE.sub("", name).strip()
    while True:
        stripped = _SUFFIX_RE.sub("", out).strip().rstrip(",")
        if stripped == out or not stripped:
            break
        out = stripped
    return out


def _cik10(cik: int) -> str:
    """SEC canonical 10-digit zero-padded CIK string."""
    return f"{cik:010d}"


def build_entities(
    rows: Iterable[Mapping[str, Any]],
    *,
    alias_overrides: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build entity records from ``company_tickers.json`` rows.

    ``rows``: dicts with ``cik_str``, ``ticker``, ``title`` (SEC schema).
    ``alias_overrides``: manual ``{alias -> TICKER}`` map (configs/aliases.yaml),
    e.g. ``{"Alphabet": "GOOGL", "Meta": "META"}``.

    Returns one record per CIK (dual-class collapsed), each ready to construct an
    ``Entity``: ``ticker``, ``cik`` (10-digit), ``canonical_name``,
    ``aliases_json``, ``cashtag``, ``exchange`` (None here; set by 0.6),
    ``active`` (True; universe membership refines it in 0.6).
    """
    overrides_by_ticker: dict[str, list[str]] = defaultdict(list)
    for alias, ticker in (alias_overrides or {}).items():
        overrides_by_ticker[ticker.strip().upper()].append(alias)

    canonical: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for row in rows:
        try:
            cik = int(row["cik_str"])
        except (KeyError, TypeError, ValueError):
            continue
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        title = str(row.get("title", "")).strip()
        if cik not in canonical:
            canonical[cik] = {"ticker": ticker, "title": title, "alt_tickers": []}
            order.append(cik)
        elif ticker != canonical[cik]["ticker"]:
            canonical[cik]["alt_tickers"].append(ticker)

    entities: list[dict[str, Any]] = []
    for cik in order:
        rec = canonical[cik]
        aliases: list[str] = []
        for candidate in (
            mechanical_alias(rec["title"]),
            *rec["alt_tickers"],
            *overrides_by_ticker.get(rec["ticker"], []),
        ):
            candidate = candidate.strip()
            if candidate and candidate != rec["ticker"] and candidate not in aliases:
                aliases.append(candidate)
        entities.append(
            {
                "ticker": rec["ticker"],
                "cik": _cik10(cik),
                "canonical_name": rec["title"],
                "aliases_json": aliases,
                "cashtag": f"${rec['ticker']}",
                "exchange": None,
                "active": True,
            }
        )
    return entities


def resolve_ticker(query: str, entities: Iterable[Mapping[str, Any]]) -> str | None:
    """Resolve a company name/alias to a canonical ticker, case-insensitively.

    Order: exact ticker match, then canonical name, then alias list. Returns None
    if nothing matches. (Full ordered entity resolution — cashtag/fuzzy — is task
    2.4; this is the seed-time convenience used by tests and simple lookups.)
    """
    q = query.strip()
    ql = q.lower()
    by_alias: dict[str, str] = {}
    for ent in entities:
        ticker = ent["ticker"]
        if q.upper() == ticker:
            return ticker
        by_alias.setdefault(ent["canonical_name"].strip().lower(), ticker)
        for alias in ent.get("aliases_json", []):
            by_alias.setdefault(alias.strip().lower(), ticker)
    return by_alias.get(ql)
