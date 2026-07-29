"""Entity resolution + directional roles (docs/ROADMAP.md task 2.4).

Ordered passes attribute a cluster's text to tickers: cashtag → exact name →
alias table → fuzzy (rapidfuzz, high threshold). A common-word cashtag blocklist
($A, $IT, $ON, $ALL, …) suppresses false positives. Ambiguous/unmatched mentions
become UnmappedMention rows feeding aliases.yaml growth and the Gate 2 metric.

Each attribution carries a ticker_role — multi-party events (M&A) attach
``acquirer``/``target`` because those have near-opposite directional implications;
everything else defaults to ``subject``. Role rules key off headline patterns
(and later filing metadata).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz, process

from pipeline.enrich.canonical import CanonicalItem, normalize_headline

# Cashtags that collide with common English words / acronyms (roadmap examples
# $A $IT $ON $ALL, plus frequent finance-chatter false positives).
DEFAULT_CASHTAG_BLOCKLIST = frozenset(
    {
        "A",
        "I",
        "IT",
        "ON",
        "ALL",
        "ARE",
        "FOR",
        "ANY",
        "AT",
        "BE",
        "BY",
        "OR",
        "SO",
        "GO",
        "UP",
        "NEW",
        "NOW",
        "ONE",
        "OUT",
        "BIG",
        "CEO",
        "CFO",
        "USA",
        "GDP",
        "EPS",
        "IPO",
        "ER",
        "DD",
        "YOLO",
        "FDA",
        "SEC",
        "AI",
        "EV",
        "PR",
        "US",
        "Q1",
        "Q2",
    }
)

# Common English words that are also single-token company names (Target, Block,
# Match, Gap, Key, …). A lone such word in a headline ("price target") is NOT an
# attribution unless corroborated (a cashtag). Complements the cashtag blocklist;
# extensible, and the unmapped log (reason='common_word') feeds curation.
DEFAULT_NAME_BLOCKLIST = frozenset(
    {
        # function / very common words
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "over",
        "under",
        "out",
        "all",
        "any",
        "one",
        "two",
        "new",
        "now",
        "big",
        "top",
        "low",
        "high",
        "up",
        "on",
        "at",
        "by",
        "or",
        "as",
        "is",
        "are",
        "be",
        "it",
        "so",
        "we",
        "us",
        "go",
        "no",
        "yes",
        # finance / headline words that collide with tickers
        "price",
        "target",
        "buy",
        "sell",
        "hold",
        "rate",
        "rates",
        "deal",
        "deals",
        "stake",
        "offer",
        "bid",
        "loss",
        "gain",
        "gains",
        "beat",
        "miss",
        "cut",
        "cuts",
        "raise",
        "raises",
        "open",
        "close",
        "block",
        "match",
        "gap",
        "well",
        "wells",
        "live",
        "real",
        "pure",
        "core",
        "main",
        "wave",
        "peak",
        "edge",
        "first",
        "next",
        "prime",
        "best",
        "data",
        "cash",
        "gold",
        "power",
        "energy",
        "capital",
        "global",
        "general",
        "group",
        "systems",
        "solutions",
        "partners",
        "resources",
        "industries",
        "technologies",
        "holdings",
        "plus",
        "max",
        "net",
        "free",
        "fast",
        "smart",
        "alpha",
        "beta",
        "bull",
        "bear",
        "run",
        "move",
        "jump",
        "drop",
        "fall",
        "rise",
        "surge",
        "plunge",
        "soar",
        "key",
        "value",
        "growth",
        "sound",
        "good",
        "kind",
        "vision",
        "focus",
        "spark",
        # Common company-name tokens whose single-word alias collides with English
        # (e.g. "News Corp" -> mechanical alias "News" -> NWSA on every "...News..."
        # headline). Multi-token names ("News Corp") still resolve.
        "news",
        "corp",
        "inc",
        "media",
        "health",
        "bank",
        "trust",
        "fund",
        "income",
        "financial",
        "international",
        "digital",
        "motors",
        "brands",
        "properties",
    }
)

_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z.\-]{0,5})\b")
_MA_VERB_RE = re.compile(
    r"\b(?:to acquire|acquires|acquired|acquiring|to buy|to purchase|"
    r"agrees to acquire|agreed to acquire|to merge with|merger with|"
    r"takeover of|acquisition of|to take over)\b"
)
FUZZY_THRESHOLD = 92


@dataclass(frozen=True)
class Match:
    ticker: str
    method: str  # cashtag | name | alias | fuzzy
    surface: str
    pos: int
    role: str = "subject"


@dataclass(frozen=True)
class Unmapped:
    mention: str
    reason: str  # blocklist | no_match | ambiguous


@dataclass
class ResolveResult:
    matches: list[Match] = field(default_factory=list)
    unmapped: list[Unmapped] = field(default_factory=list)

    @property
    def tickers(self) -> list[str]:
        return [m.ticker for m in self.matches]


def _entity_fields(entity: Any) -> tuple[str, str, list[str]]:
    """(ticker, canonical_name, aliases) from an ORM row or a plain dict."""
    if isinstance(entity, Mapping):
        return (
            entity["ticker"],
            entity.get("canonical_name", ""),
            list(entity.get("aliases_json", []) or []),
        )
    return (
        entity.ticker,
        getattr(entity, "canonical_name", "") or "",
        list(getattr(entity, "aliases_json", []) or []),
    )


class EntityResolver:
    def __init__(
        self,
        entities: Iterable[Any],
        *,
        blocklist: Iterable[str] = DEFAULT_CASHTAG_BLOCKLIST,
        name_blocklist: Iterable[str] = DEFAULT_NAME_BLOCKLIST,
        fuzzy_threshold: int = FUZZY_THRESHOLD,
    ) -> None:
        self._tickers: set[str] = set()
        self._name_index: dict[str, str] = {}  # normalized name/alias -> ticker
        self._name_kind: dict[str, str] = {}  # normalized key -> 'name' | 'alias'
        for entity in entities:
            ticker, canonical_name, aliases = _entity_fields(entity)
            self._tickers.add(ticker)
            if canonical_name:
                key = normalize_headline(canonical_name)
                if key:
                    self._name_index.setdefault(key, ticker)
                    self._name_kind.setdefault(key, "name")
            for alias in aliases:
                key = normalize_headline(alias)
                if key and key not in self._name_index:
                    self._name_index[key] = ticker
                    self._name_kind[key] = "alias"
        self._blocklist = {b.upper() for b in blocklist}
        self._name_blocklist = {w.lower() for w in name_blocklist}
        self._fuzzy_threshold = fuzzy_threshold
        # Longest name/alias in tokens, capped — bounds the n-gram enumeration.
        self._max_ngram = min(max((len(k.split()) for k in self._name_index), default=1), 8)

    def cashtag_tickers(self, text: str) -> list[str]:
        """Fast cashtag-only match for high-volume firehose filtering: the real-
        universe tickers a text names via ``$SYM``, blocklist-guarded, with NO
        name/alias/fuzzy passes (those are too costly per-post and fuzzy would
        false-match noise). Returns distinct tickers in first-seen order; empty
        list means "not about a real ticker" (drop the post)."""
        out: list[str] = []
        seen: set[str] = set()
        for m in _CASHTAG_RE.finditer(text):
            sym = m.group(1).upper().replace(".", "-")
            if sym in seen or sym in self._blocklist or sym not in self._tickers:
                continue
            seen.add(sym)
            out.append(sym)
        return out

    def resolve(self, text: str) -> ResolveResult:
        result = ResolveResult()
        claimed: dict[str, Match] = {}  # ticker -> best match
        norm = normalize_headline(text)

        # Pass 1 — cashtags (with blocklist + fuzzy fallback).
        for m in _CASHTAG_RE.finditer(text):
            sym = m.group(1).upper().replace(".", "-")
            if sym in self._blocklist:
                result.unmapped.append(Unmapped(f"${sym}", "blocklist"))
                continue
            if sym in self._tickers:
                claimed.setdefault(sym, Match(sym, "cashtag", f"${sym}", m.start()))
            else:
                best = process.extractOne(sym, list(self._tickers), scorer=fuzz.ratio)
                if best and best[1] >= self._fuzzy_threshold:
                    claimed.setdefault(best[0], Match(best[0], "fuzzy", f"${sym}", m.start()))
                else:
                    result.unmapped.append(Unmapped(f"${sym}", "no_match"))

        # Passes 2/3 — exact name then alias via token n-gram lookup. O(tokens),
        # not O(entities): enumerate whole-token n-grams of the headline and hit
        # the index (longest phrase at each position wins). This replaces a per-key
        # regex scan that recompiled ~15k patterns per cluster at real entity scale.
        tokens = [(m.group(0), m.start()) for m in re.finditer(r"\S+", norm)]
        guarded: list[tuple[str, str]] = []  # (surface, ticker) common-word single tokens
        for i in range(len(tokens)):
            upper = min(self._max_ngram, len(tokens) - i)
            for n in range(upper, 0, -1):
                phrase = " ".join(w for w, _ in tokens[i : i + n])
                ticker = self._name_index.get(phrase)
                if ticker is None:
                    continue
                if ticker not in claimed:
                    # Guard: a lone common English word that is also a company name
                    # ("target" in "price target") is not an attribution unless
                    # corroborated (a cashtag already claimed this ticker in pass 1).
                    if n == 1 and phrase in self._name_blocklist:
                        guarded.append((phrase, ticker))
                    else:
                        claimed[ticker] = Match(
                            ticker, self._name_kind[phrase], phrase, tokens[i][1]
                        )
                break  # most-specific (longest) phrase at this position resolved

        for surface, ticker in guarded:
            if ticker not in claimed:  # unclaimed by cashtag or a distinctive phrase
                result.unmapped.append(Unmapped(surface, "common_word"))

        result.matches = _assign_roles(norm, list(claimed.values()))
        return result


def _assign_roles(norm_text: str, matches: list[Match]) -> list[Match]:
    verb = _MA_VERB_RE.search(norm_text)
    if verb is None or len(matches) < 2:
        return [Match(m.ticker, m.method, m.surface, m.pos, "subject") for m in matches]
    # M&A: entity mentioned before the verb is the acquirer, after it the target.
    vpos = verb.start()
    out = []
    for m in matches:
        role = "acquirer" if m.pos < vpos else "target"
        out.append(Match(m.ticker, m.method, m.surface, m.pos, role))
    return out


def resolve_cluster(item: CanonicalItem, resolver: EntityResolver) -> ResolveResult:
    """Resolve a cluster's origin item — title plus any cashtags in the body."""
    text = item.title
    if item.description:
        # Cashtags often live in the body; names usually in the headline.
        cashtags = " ".join(m.group(0) for m in _CASHTAG_RE.finditer(item.description))
        if cashtags:
            text = f"{text} {cashtags}"
    return resolver.resolve(text)
