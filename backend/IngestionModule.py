"""
IngestionModule.py
==================
Real-time financial news ingestion agent.

Stage 1 — RSS Extraction
    Polls a configurable set of financial newswire RSS feeds on a defined
    interval and extracts: title, published time, and description for every
    new item.  Duplicate detection is handled via a content-hash cache so
    re-fetched feeds never emit the same article twice.

Stage 2 — SEC EDGAR Integration
    Polls the EDGAR full-text search / latest-filings RSS feed for 8-K, 10-K,
    10-Q, and S-1 filings.  Each item is enriched with filing type, accession
    number, and company name where available.

Stage 3 — FDA Integration
    Polls the openFDA drug-event and drug-enforcement endpoints (REST JSON)
    plus the official FDA news RSS feed.  Returns structured NewsItem objects
    alongside the raw FDA payload.

Architecture
------------
• Every source produces ``NewsItem`` dataclass objects.
• A shared ``asyncio.Queue`` receives all items from all sources.
• A ``DispatchRouter`` sits downstream of the queue and fans items out to
  registered handler callbacks (persist to DB, push to WebSocket, etc.).
• The ``IngestionAgent`` orchestrates lifecycle: start / stop / status.

Usage
-----
    import asyncio
    from IngestionModule import IngestionAgent, NewsItem

    async def my_handler(item: NewsItem) -> None:
        print(item)

    agent = IngestionAgent()
    agent.dispatcher.register(my_handler)

    asyncio.run(agent.run())          # blocks; Ctrl-C to stop
    # — or —
    asyncio.run(agent.start())        # non-blocking background tasks
    await asyncio.sleep(60)
    await agent.stop()

Dependencies
------------
    pip install aiohttp feedparser python-dateutil
"""

from __future__ import annotations

import asyncio
import calendar
import csv
import hashlib
import html
import itertools
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

import aiohttp
import feedparser
from dateutil import parser as dateutil_parser

# Gate 1 refit (docs/ROADMAP.md task 1.3): the EDGAR fair-access UA resolver and
# the client-side rate limiter live in the pipeline package the ingestors are
# being refit onto.
from pipeline.ingest.edgar import edgar_user_agent
from pipeline.ingest.ratelimit import RateLimiter

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("ingestion_agent")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewsItem:
    """Normalised news item produced by every ingestion source."""

    source: str                          # human-readable source label
    source_type: str                     # "rss" | "sec" | "fda"
    title: str
    published_at: datetime               # always UTC-aware
    description: str
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    topic: str = ""                      # assigned by TopicClassifier at dispatch time
    tickers: tuple[str, ...] = field(default=(), hash=False, compare=False)

    # Stable identity hash for deduplication
    @property
    def content_hash(self) -> str:
        raw = f"{self.source}|{self.title}|{self.url}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def __str__(self) -> str:
        ts = self.published_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            f"[{self.source_type.upper()}] [{self.source}] {ts}\n"
            f"  Topic : {self.topic or 'Unclassified'}\n"
            f"  Title : {self.title}\n"
            f"  URL   : {self.url}\n"
            f"  Desc  : {self.description[:160]}{'…' if len(self.description) > 160 else ''}"
        )


# ---------------------------------------------------------------------------
# Duplicate-item cache
# ---------------------------------------------------------------------------

class _SeenCache:
    """Thread-safe (asyncio-safe) LRU-style seen-hash cache."""

    def __init__(self, maxsize: int = 50_000) -> None:
        self._cache: dict[str, float] = {}
        self._maxsize = maxsize

    def is_new(self, item: NewsItem) -> bool:
        h = item.content_hash
        if h in self._cache:
            return False
        if len(self._cache) >= self._maxsize:
            # asyncio is single-threaded, so dict insertion order == arrival order;
            # islice evicts the oldest 25% in O(n) without a sort.
            to_evict = list(itertools.islice(self._cache, self._maxsize // 4))
            for k in to_evict:
                del self._cache[k]
        self._cache[h] = time.monotonic()
        return True


# ---------------------------------------------------------------------------
# Keyword filter
# ---------------------------------------------------------------------------

# ┌─────────────────────────────────────────────────────────────────────────┐
# │  FILTER_KEYWORDS — edit this list to control what gets dispatched.      │
# │                                                                         │
# │  • Each string is matched case-insensitively against an item's title    │
# │    and description.  An item passes if ANY keyword matches.             │
# │  • Add new keywords anywhere in the list, or add a new comment-grouped  │
# │    section following the pattern below.                                 │
# │  • To disable filtering entirely, pass keywords=[] to IngestionAgent.   │
# └─────────────────────────────────────────────────────────────────────────┘
FILTER_KEYWORDS: list[str] = [
    # Macro / monetary policy
    "inflation", "interest rate", "federal reserve", "fed",
    # Equities & corporate events
    "earnings", "revenue", "stock", "ipo", "merger", "acquisition", "buyback",
    # Regulatory / legal
    "sec filing", "lawsuit", "settlement", "investigation",
    # FDA / pharma
    "fda", "recall", "drug approval", "clinical trial",
    # Short-seller / activist research ("short" is substring-matched, so it
    # also catches "shorting", "short thesis", "MW is Short …")
    "short", "fraud", "overvalued", "misleading investors",
    # Crypto / digital assets
    "bitcoin", "crypto", "ethereum", "blockchain",
]


class KeywordFilter:
    """
    Accepts a NewsItem only if at least one keyword appears in its title or
    description (case-insensitive).  An empty keyword list disables filtering
    and passes every item through.
    """

    def __init__(self, keywords: list[str]) -> None:
        # Lowercase once at construction so per-item matching is cheap
        self._keywords = [kw.lower() for kw in keywords]

    def accepts(self, item: NewsItem) -> bool:
        if not self._keywords:
            return True
        haystack = (item.title + " " + item.description).lower()
        return any(kw in haystack for kw in self._keywords)


# ---------------------------------------------------------------------------
# Topic classifier
# ---------------------------------------------------------------------------

# ┌─────────────────────────────────────────────────────────────────────────┐
# │  TOPIC_KEYWORDS — edit this dict to add, remove, or rename topics.      │
# │                                                                          │
# │  • Each key is the topic label that will appear on the NewsItem.        │
# │  • Each value is a list of keywords (case-insensitive) for that topic.  │
# │  • Topics are checked in order — the first match wins.                  │
# │  • Items that match no topic are labelled "General".                    │
# └─────────────────────────────────────────────────────────────────────────┘
TOPIC_KEYWORDS: dict[str, list[str]] = {
    # Crypto / digital assets
    "Crypto": ["bitcoin", "ethereum", "crypto", "blockchain", "defi", "nft", "altcoin"],
    # Energy & commodities
    "Energy": ["oil", "gas", "opec", "crude", "energy", "renewable", "solar", "pipeline"],
    # Equities & corporate events
    "Equities": ["earnings", "ipo", "stock", "shares", "dividend", "buyback", "merger", "acquisition"],
    # Macro / monetary policy
    "Macro": ["inflation", "interest rate", "federal reserve", "fed", "gdp", "recession", "cpi"],
    # Regulatory / legal
    "Regulatory": ["sec", "fda", "recall", "enforcement", "lawsuit", "settlement", "investigation"],
    # Fixed income
    "Bonds": ["treasury", "yield", "bond", "debt", "credit rating", "sovereign"],
    # Commodities
    "Commodities": ["gold", "silver", "copper", "wheat", "corn", "commodity", "futures"],
    # Technology
    "Technology": ["ai", "semiconductor", "chip", "software", "cloud", "tech", "cybersecurity"],
}


class TopicClassifier:
    """
    Assigns a topic label to a NewsItem by matching its title and description
    against TOPIC_KEYWORDS.  The first matching topic wins.  Items that match
    nothing are labelled "General".
    """

    def __init__(self, topics: dict[str, list[str]] | None = None) -> None:
        src = topics if topics is not None else TOPIC_KEYWORDS
        # Pre-lowercase all keywords once at construction
        self._topics: list[tuple[str, list[str]]] = [
            (label, [kw.lower() for kw in keywords])
            for label, keywords in src.items()
        ]

    def classify(self, item: NewsItem) -> str:
        haystack = (item.title + " " + item.description).lower()
        matches = [
            label for label, keywords in self._topics
            if any(kw in haystack for kw in keywords)
        ]
        return ", ".join(matches) if matches else "General"


# ---------------------------------------------------------------------------
# Dispatch router
# ---------------------------------------------------------------------------

Handler = Callable[[NewsItem], Coroutine[Any, Any, None]]


class DispatchRouter:
    """Fan-out dispatcher: delivers each NewsItem to all registered handlers."""

    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def register(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def dispatch(self, item: NewsItem) -> None:
        for handler in self._handlers:
            try:
                await handler(item)
            except Exception as exc:  # noqa: BLE001
                log.error("Handler %s raised: %s", handler, exc)


# ---------------------------------------------------------------------------
# Shared HTTP session helper
# ---------------------------------------------------------------------------

# Shared TLS context from certifi's CA bundle. Python/aiohttp on Windows doesn't
# always see a complete CA store, so some issuers (CFTC, Stock Titan) fail chain
# validation against the system default; certifi carries the intermediates.
try:
    import ssl as _ssl
    import certifi as _certifi
    _SSL_CTX = _ssl.create_default_context(cafile=_certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CTX = None

# A real-browser User-Agent for the handful of publishers that block or tarpit
# our contact UA (Endpoints News 403s it; Nasdaq's rssoutbound hangs on it).
# Applied PER FEED via a feed's "user_agent" key — never globally: SEC's
# fair-access policy wants the contact UA and Reddit 429s browser UAs.
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


class _HttpClient:
    """Thin wrapper around aiohttp.ClientSession with shared headers."""

    # SEC's fair-access policy asks every automated client to send a User-Agent
    # that identifies the operator with a real contact (task 1.3). Set
    # EDGAR_USER_AGENT (preferred) or legacy SEC_CONTACT_EMAIL in the environment;
    # an unset deployment falls back to a clearly-placeholder value SEC throttles.
    _ACCEPT = "application/rss+xml, application/xml, text/xml, application/json, */*"

    @staticmethod
    def _build_headers() -> dict[str, str]:
        # Resolved at session-creation time (env may be set after import). Per-feed
        # browser-UA overrides still apply for publishers that block the contact UA.
        return {"User-Agent": edgar_user_agent(), "Accept": _HttpClient._ACCEPT}

    def __init__(self, timeout: int = 15) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "_HttpClient":
        # Use certifi's CA bundle for TLS validation when available.
        connector = aiohttp.TCPConnector(ssl=_SSL_CTX) if _SSL_CTX is not None else None
        self._session = aiohttp.ClientSession(
            headers=self._build_headers(),
            timeout=self._timeout,
            connector=connector,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()

    async def get_text(self, url: str, headers: dict | None = None) -> str:
        text, _ = await self.get_text_and_headers(url, headers=headers)
        return text

    async def get_text_and_headers(
        self, url: str, headers: dict | None = None
    ) -> tuple[str, Any]:
        """GET returning (body_text, response_headers) — headers let callers read
        rate-limit signals (e.g. Reddit's x-ratelimit-*)."""
        if self._session is None:
            raise RuntimeError("_HttpClient must be used as an async context manager")
        # Per-request headers override the session defaults (e.g. a per-feed UA).
        async with self._session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            text = await resp.text()
            return text, resp.headers

    async def get_json(self, url: str, params: dict | None = None) -> Any:
        if self._session is None:
            raise RuntimeError("_HttpClient must be used as an async context manager")
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


# ---------------------------------------------------------------------------
# Utility: parse dates robustly
# ---------------------------------------------------------------------------

def _parse_dt(raw: Any) -> datetime:
    """Return a UTC-aware datetime from a feedparser time-struct or string."""
    if raw is None:
        return datetime.now(tz=timezone.utc)
    # feedparser exposes parsed time as a time.struct_time
    if hasattr(raw, "tm_year"):
        ts = calendar.timegm(raw)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        dt = dateutil_parser.parse(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.now(tz=timezone.utc)


def _strip_html(text: str) -> str:
    """Strip HTML tags and decode HTML entities. Used to clean Reddit RSS descriptions."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Stage 1 — RSS Extractor
# ---------------------------------------------------------------------------

# Reddit's unauthenticated RSS rate limit is PER REQUEST (verified via the
# x-ratelimit-* response headers: one GET → used=1, remaining=0, reset≈35s), so
# fetching 17 subreddits one-by-one is 17 requests. Reddit supports MULTI-subreddit
# feeds — ``r/a+b+c/new/.rss`` returns posts from every listed sub in ONE request —
# so we group the subreddits and poll a group per request instead. Groups are
# balanced so a very high-volume sub (WSB, crypto) doesn't crowd quieter ones out
# of the merged /new window. Per-post subreddit attribution is recovered from each
# entry's link at parse time, so stored docs keep a real ``Reddit - <sub>`` source.
REDDIT_GROUPS: list[list[str]] = [
    ["wallstreetbets", "SecurityAnalysis", "ValueInvesting", "dividends"],
    ["stocks", "economics", "algotrading", "thetagang"],
    ["options", "econmonitor", "Daytrading", "Shortsqueeze"],
    ["CryptoCurrency", "Bitcoin", "investing", "StockMarket", "pennystocks"],
]


def _reddit_group_feeds() -> list[dict[str, str]]:
    """Build one combined multi-subreddit RSS feed per group in REDDIT_GROUPS."""
    return [
        {
            "label": f"Reddit - Group {i}",
            "url": "https://www.reddit.com/r/" + "+".join(subs) + "/new/.rss",
            "source_type": "social",
            "reddit_group": True,
        }
        for i, subs in enumerate(REDDIT_GROUPS, 1)
    ]


#: Default financial newswire RSS feeds
DEFAULT_RSS_FEEDS: list[dict[str, str]] = [
    # ── Major wires ──────────────────────────────────────────────────────────
    {
        "label": "Bloomberg Markets",
        "url": "https://feeds.bloomberg.com/markets/news.rss",
    },
    {
        "label": "Financial Times",
        "url": "https://www.ft.com/rss/home",
    },
    {
        "label": "Wall Street Journal Markets",
        "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    },
    {
        "label": "CNBC Top News",
        "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    },
    {
        "label": "MarketWatch Top Stories",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
    },
    {
        "label": "Seeking Alpha Market News",
        "url": "https://seekingalpha.com/market_currents.xml",
    },
    {
        # Dow Jones public feed - fires intraday as headlines break, well
        # before topstories rotates. Verified live 2026-07-05.
        "label": "MarketWatch Real-time Headlines",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    },
    {
        # Terse market-moving one-liners (halts, M&A flashes, macro prints).
        "label": "MarketWatch Bulletins",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_bulletins",
    },
    {
        # High-volume: analyst ratings, company news, macro. Verified live
        # 2026-07-05 (analyst actions appear within minutes).
        "label": "Investing.com News",
        "url": "https://www.investing.com/rss/news.rss",
    },
    {
        # Interactive Brokers Traders' Insight / IBKR Campus — first-party, free,
        # public RSS: market commentary, macro/econ notes, prediction-market reads.
        # Verified live 2026-07-31: 20 entries, all with guid + title + clean
        # first-party url + timestamp, fresh (same-day), 200 under the contact UA.
        "label": "IBKR Traders' Insight",
        "url": "https://www.interactivebrokers.com/campus/feed/",
    },
    # ── Macro / economic data (primary government sources) ───────────────────
    {
        "label": "Federal Reserve Press Releases",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
    },
    {
        "label": "BLS Economic News",
        "url": "https://www.bls.gov/feed/bls_latest.rss",
    },
    {
        "label": "Bureau of Economic Analysis",  # GDP, PCE, trade balance (source data)
        "url": "https://apps.bea.gov/rss/rss.xml",
    },
    {
        "label": "EIA Today in Energy",  # oil/gas/power supply-demand (source data)
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
    },
    # ── Regulators (primary enforcement / antitrust sources) ─────────────────
    {
        "label": "CFTC Press Releases",  # derivatives/commodity enforcement
        "url": "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
    },
    {
        "label": "FTC Press Releases",  # antitrust / merger challenges
        "url": "https://www.ftc.gov/feeds/press-release.xml",
    },
    {
        # Indictments, merger suits, FCPA settlements - frequent single-name
        # catalysts. Verified live 2026-07-05.
        "label": "DOJ Press Releases",
        "url": "https://www.justice.gov/news/rss?type=press_release&m=1",
    },
    # ── Crypto / digital assets ──────────────────────────────────────────────
    {
        "label": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    },
    {
        "label": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
    },
    # ── Equities / analysis ──────────────────────────────────────────────────
    {
        "label": "Yahoo Finance",
        "url": "https://finance.yahoo.com/rss/topfinstories",
    },
    # ── Press release wires ──────────────────────────────────────────────────
    {
        "label": "PR Newswire",
        "url": "https://www.prnewswire.com/rss/news-releases-list.rss",
    },
    {
        "label": "Business Wire",
        "url": "https://feed.businesswire.com/rss/home/?rss=G1&rssid=1",
    },
    {
        "label": "Benzinga",
        "url": "https://www.benzinga.com/feed",
    },
    {
        "label": "GlobeNewswire",
        "url": (
            "https://www.globenewswire.com/RssFeed/orgclass/1/"
            "feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"
        ),
    },
    {
        # Aggregates BusinessWire/GlobeNewswire/ACCESSWIRE press releases with
        # ticker tags - partially recovers ACCESSWIRE coverage (see NOTE below)
        # through a fetchable aggregator. Verified live 2026-07-05.
        "label": "Stock Titan",
        "url": "https://www.stocktitan.net/rss",
    },
    # ── Exchange operations (market-structure catalysts) ─────────────────────
    {
        # Ticker-level halt/resume events with reason codes (T1 news pending,
        # T12 info requested, H11 regulatory). A T12/H11 halt is itself a
        # catalyst; item titles are bare symbols. Verified live 2026-07-05.
        "label": "Nasdaq Trade Halts",
        "url": "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts",
        # Bare-symbol titles ("JZ") carry no finance keywords, so this feed
        # dies at the relevance filter unless exempted — a halt row is
        # inherently financial, same argument as the SEC/FDA exemption.
        "always_dispatch": True,
    },
    {
        "label": "Nasdaq Markets",
        "url": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
        # nasdaq.com/feed/rssoutbound tarpits non-browser UAs (hangs to timeout);
        # a browser UA returns immediately.
        "user_agent": _BROWSER_UA,
    },
    {
        # New listings / pricing - fresh-ticker catalysts the general wires
        # cover late. Verified live 2026-07-05.
        "label": "Nasdaq IPOs",
        "url": "https://www.nasdaq.com/feed/rssoutbound?category=IPOs",
        "user_agent": _BROWSER_UA,
    },
    # ── Biotech / pharma verticals (FDA-catalyst depth) ──────────────────────
    # Trade press breaks trial readouts, CRLs, and approval odds context the
    # FDA's own feeds lack. All four verified live 2026-07-05.
    {
        "label": "FierceBiotech",
        "url": "https://www.fiercebiotech.com/rss/xml",
    },
    {
        "label": "FiercePharma",
        "url": "https://www.fiercepharma.com/rss/xml",
    },
    {
        "label": "Endpoints News",
        "url": "https://endpoints.news/feed",
        # 403s our contact UA (bot filter); a browser UA is served normally.
        "user_agent": _BROWSER_UA,
    },
    {
        "label": "BioPharma Dive",
        "url": "https://www.biopharmadive.com/feeds/news/",
    },
    # NOTE: ACCESSWIRE / ACCESS Newswire is intentionally NOT listed. Their site
    # (accesswire.com / accessnewswire.com) sits behind a Cloudflare bot
    # challenge that returns HTTP 403 "Just a moment..." to any server-side
    # fetch, so it cannot be ingested via a plain RSS poll. Re-add here if a
    # licensed/API feed URL becomes available.
    # ── Short-seller / activist research ─────────────────────────────────────
    # Major bearish catalysts (short_seller_report in the deep-read taxonomy)
    # publish on research-shop blogs, not wires — without these feeds the
    # catalyst lanes are structurally blind to them. All four probed live
    # 2026-07-03 (HTTP 200 + RSS XML). Muddy Waters, Viceroy, and Ningi block
    # server-side fetches (timeout/404/Cloudflare 403) and are intentionally
    # omitted; Hindenburg disbanded Jan 2025.
    {
        "label": "Grizzly Research",
        "url": "https://grizzlyreports.com/feed/",
    },
    {
        "label": "The Bear Cave",
        "url": "https://thebearcave.substack.com/feed",
    },
    {
        "label": "Kerrisdale Capital",
        "url": "https://www.kerrisdalecap.com/feed/",
    },
    {
        "label": "Blue Orca Capital",
        "url": "https://blueorcacapital.com/feed/",
    },
    # ── SEC regulatory news (RSS) — routed to the "sec" lane so it joins EDGAR
    # filings in the regulatory catalyst profile and skips the keyword filter.
    {
        "label": "SEC Press Releases",
        "url": "https://www.sec.gov/news/pressreleases.rss",
        "source_type": "sec",
    },
    {
        "label": "SEC Administrative Proceedings",  # enforcement actions
        "url": "https://www.sec.gov/rss/litigation/admin.xml",
        "source_type": "sec",
    },
    # ── Reddit social feeds ──────────────────────────────────────────────────
    # Reddit is appended below as a few COMBINED multi-subreddit feeds (see
    # REDDIT_GROUPS / _reddit_group_feeds) rather than one feed per subreddit, so
    # all subreddits are covered in a handful of requests. source_type="social"
    # routes them to the frontend's Social panel.
] + _reddit_group_feeds()

# ---------------------------------------------------------------------------
# Unstructured source feed configs (used by future UnstructuredModule.py)
# ---------------------------------------------------------------------------

#: StockTwits ticker watchlist — polled via public symbol stream endpoint.
#: Crypto uses the .X suffix convention StockTwits requires.
STOCKTWITS_WATCHLIST: list[str] = [
    # Broad market ETFs
    "SPY", "QQQ", "DIA",
    # Mega-cap equities
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    # Financials
    "JPM", "BAC",
    # Energy
    "XOM", "CVX",
    # Technology / semiconductors
    "AMD", "INTC",
    # Bonds / macro proxies
    "TLT", "GLD",
    # Crypto
    "BTC.X", "ETH.X",
    # High-sentiment / retail-watched
    "GME", "AMC", "PLTR",
]

#: Bluesky keyword/hashtag search terms — queried via public AT Protocol API.
BLUESKY_SEARCH_TERMS: list[str] = [
    # Equities & general market
    "#stocks", "#investing", "#stockmarket", "#wallstreetbets", "#earnings",
    "#trading", "#options", "#ipo", "#merger",
    # Macro
    "#economy", "#inflation", "#federalreserve", "#gdp", "#cpi",
    # Crypto
    "#crypto", "#bitcoin", "#ethereum", "#defi",
    # Commodities / energy
    "#gold", "#oil", "#commodities",
    # Bonds
    "#bonds", "#treasury",
    # Tech / sector
    "#fintech", "#semiconductor", "#ai",
    # Curated NON-cashtag phrases — the term-search lane's distinctive coverage
    # that the universe-wide cashtag firehose can't get (event language, not $TICKERs).
    "short squeeze", "earnings beat", "profit warning", "guidance cut",
    "stock split", "buyback", "insider buying", "bankruptcy filing",
]


class RSSExtractor:
    """
    Polls a list of RSS feeds on a configurable interval.

    For each new feed entry it emits a ``NewsItem`` with:
        • title       — entry title
        • published_at — parsed publication timestamp (UTC)
        • description  — summary / content snippet
    """

    def __init__(
        self,
        feeds: list[dict[str, str]] | None = None,
        poll_interval: float = 60.0,
        queue: asyncio.Queue | None = None,
        seen_cache: _SeenCache | None = None,
        max_age_minutes: float | None = None,
        social_feed_delay: float = 2.0,
        reddit_per_cycle: int = 1,
    ) -> None:
        self.feeds = feeds or DEFAULT_RSS_FEEDS
        self.poll_interval = poll_interval
        self.queue: asyncio.Queue = queue or asyncio.Queue()
        self._seen = seen_cache or _SeenCache()
        # Drop items published more than this many minutes ago; None = no limit
        self.max_age_minutes = max_age_minutes
        # Seconds to wait between sequential social-feed requests (avoids Reddit 429s)
        self.social_feed_delay = social_feed_delay
        # Reddit's unauthenticated RSS rate-limits hard — measured ~1 request per
        # ~30 s per IP, so bursting all subreddits each cycle 429s everything after
        # the first. Instead poll only this many Reddit feeds per cycle and rotate
        # through the list round-robin (full sweep = len(reddit) * poll_interval).
        self.reddit_per_cycle = max(1, reddit_per_cycle)
        self._social_cursor = 0
        # Adaptive pacing: earliest monotonic time the next Reddit request is
        # allowed. Updated from Reddit's x-ratelimit-reset header after each poll,
        # so we self-throttle exactly to Reddit's stated window instead of guessing.
        self._reddit_next_ok = 0.0
        self._running = False

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_description(entry: Any) -> str:
        """Return the best available description text from a feed entry."""
        for attr in ("summary", "description", "content"):
            val = getattr(entry, attr, None)
            if val:
                # feedparser wraps 'content' in a list of dicts
                if isinstance(val, list):
                    val = " ".join(v.get("value", "") for v in val)
                return str(val).strip()
        return ""

    async def _poll_feed(
        self, feed_cfg: dict[str, str], http: _HttpClient
    ) -> list[NewsItem]:
        label = feed_cfg["label"]
        url = feed_cfg["url"]
        items: list[NewsItem] = []
        # A feed may pin its own User-Agent (publishers that block/tarpit our
        # contact UA — e.g. Endpoints News, Nasdaq rssoutbound).
        ua = feed_cfg.get("user_agent")
        req_headers = {"User-Agent": ua} if ua else None
        is_reddit_group = bool(feed_cfg.get("reddit_group"))
        try:
            raw_xml, resp_headers = await http.get_text_and_headers(url, headers=req_headers)
            # Reddit tells us our remaining budget + when it resets; pace off that.
            if is_reddit_group:
                self._note_reddit_ratelimit(resp_headers)
            parsed = feedparser.parse(raw_xml)
            for entry in parsed.entries:
                title = getattr(entry, "title", "").strip() or "(no title)"
                link = getattr(entry, "link", "")
                pub_raw = getattr(entry, "published_parsed", None) or getattr(
                    entry, "updated_parsed", None
                )
                published_at = _parse_dt(pub_raw)
                description = self._extract_description(entry)
                if feed_cfg.get("source_type") == "social":
                    description = _strip_html(description)

                # Combined Reddit feeds carry posts from several subreddits; recover
                # the real subreddit from the entry link so the stored source stays
                # "Reddit - <sub>" instead of the generic group label.
                source = label
                if is_reddit_group:
                    m = re.search(r"/r/([A-Za-z0-9_]+)/", link)
                    if m:
                        source = f"Reddit - {m.group(1)}"

                # feed_cfg may override source_type (e.g. Reddit feeds use "social")
                item = NewsItem(
                    source=source,
                    source_type=feed_cfg.get("source_type", "rss"),
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=link,
                    # guid feeds the raw_items deterministic id sha256(source, guid|url)
                    # (Gate 1 task 1.1); feedparser exposes <guid>/entry.id as entry.id.
                    extra={"guid": getattr(entry, "id", "") or link},
                )
                if self.max_age_minutes is not None:
                    age = (datetime.now(tz=timezone.utc) - published_at).total_seconds() / 60
                    if age > self.max_age_minutes:
                        continue
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("RSS poll failed [%s]: %s", label, exc)
        return items

    def _note_reddit_ratelimit(self, headers: Any) -> None:
        """Set the next-allowed Reddit request time from x-ratelimit-* headers."""
        try:
            remaining = float(headers.get("x-ratelimit-remaining", "1") or "1")
            reset = float(headers.get("x-ratelimit-reset", "0") or "0")
        except (TypeError, ValueError):
            return
        if remaining < 1.0 and reset > 0:
            # +1s safety margin over Reddit's stated reset window.
            self._reddit_next_ok = time.monotonic() + reset + 1.0

    async def _await_reddit_budget(self) -> None:
        """Sleep until Reddit's rate-limit window has reset (adaptive pacing)."""
        wait = self._reddit_next_ok - time.monotonic()
        if wait > 0:
            log.info("RSSExtractor — Reddit rate-limit pacing: waiting %.0fs", wait)
            await asyncio.sleep(wait)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Run forever, polling all feeds every ``poll_interval`` seconds."""
        self._running = True
        standard = [f for f in self.feeds if f.get("source_type", "rss") != "social"]
        social = [f for f in self.feeds if f.get("source_type") == "social"]
        log.info(
            "RSSExtractor started — %d standard + %d social feeds, interval=%ss",
            len(standard), len(social), self.poll_interval,
        )
        async with _HttpClient() as http:
            while self._running:
                start = asyncio.get_running_loop().time()
                total = 0

                # Standard RSS: fire all concurrently (newswires tolerate it)
                if standard:
                    results = await asyncio.gather(
                        *[self._poll_feed(f, http) for f in standard],
                        return_exceptions=True,
                    )
                    for batch in results:
                        if isinstance(batch, list):
                            for item in batch:
                                await self.queue.put(item)
                                total += 1

                # Social RSS (Reddit): rotate a slice per cycle (combined feeds, so
                # a "feed" is a whole subreddit group) and self-throttle to Reddit's
                # own x-ratelimit-reset so we never trip a 429.
                if social:
                    n = min(self.reddit_per_cycle, len(social))
                    picked = [social[(self._social_cursor + i) % len(social)] for i in range(n)]
                    self._social_cursor = (self._social_cursor + n) % len(social)
                    for i, feed in enumerate(picked):
                        if not self._running:
                            break
                        await self._await_reddit_budget()
                        got = 0
                        for item in await self._poll_feed(feed, http):
                            await self.queue.put(item)
                            total += 1
                            got += 1
                        log.info("RSSExtractor — Reddit %s: %d new (rotation %d/%d)",
                                 feed.get("label", "?"), got,
                                 self._social_cursor or len(social), len(social))
                        if i < n - 1:
                            await self._await_reddit_budget()

                log.info("RSSExtractor — cycle complete, %d new items", total)
                elapsed = asyncio.get_running_loop().time() - start
                await asyncio.sleep(max(0.0, self.poll_interval - elapsed))

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Stage 2 — SEC EDGAR Extractor
# ---------------------------------------------------------------------------

#: EDGAR latest-filings RSS (covers all filing types)
_EDGAR_RSS_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={filing_type}&dateb=&owner=include&count=40&search_text=&output=atom"

#: Filing types of primary interest to financial-news dashboards.
#: Periodic/registration (8-K…6-K) plus the M&A / activist / offering forms that
#: are themselves hard catalysts: 425 & S-4 (merger comms/registration), SC 13D
#: (>5% activist stake) + /A amendments, SC TO-T / SC 14D9 (tender offer & target
#: response), DEFM14A (merger vote proxy), 424B4 (priced offering — dilution).
DEFAULT_SEC_FILING_TYPES: list[str] = [
    "8-K", "10-K", "10-Q", "S-1", "6-K",
    "425", "S-4", "SC 13D", "SC 13D/A", "SC TO-T", "SC 14D9", "DEFM14A", "424B4",
    # Amendments worth their own poll: S-1/A carries the terms/pricing updates
    # on the IPO path; 8-K/A is the *correction* of a material disclosure.
    # Same-window dedup is handled downstream — EDGAR titles for a base filing
    # and its /A share nearly all tokens, so they title-cluster together.
    "S-1/A", "8-K/A",
]


class SECExtractor:
    """
    Polls SEC EDGAR for new regulatory filings.

    Each filing is normalised into a ``NewsItem`` (source_type="sec") with
    the filing type, accession number, and filer name embedded in
    ``extra``.
    """

    def __init__(
        self,
        filing_types: list[str] | None = None,
        poll_interval: float = 300.0,   # EDGAR asks for ≥ 10 s between requests
        queue: asyncio.Queue | None = None,
        seen_cache: _SeenCache | None = None,
        edgar_max_per_sec: float = 10.0,
    ) -> None:
        self.filing_types = filing_types or DEFAULT_SEC_FILING_TYPES
        self.poll_interval = poll_interval
        self.queue: asyncio.Queue = queue or asyncio.Queue()
        self._seen = seen_cache or _SeenCache()
        # Client-side EDGAR rate limit ≤10 req/s (task 1.3), on top of the
        # per-cycle courtesy stagger in run().
        self._limiter = RateLimiter(edgar_max_per_sec)
        self._running = False

    async def _poll_filing_type(
        self, filing_type: str, http: _HttpClient
    ) -> list[NewsItem]:
        # Forms like "SC 13D/A" carry spaces & slashes — encode for the query string.
        url = _EDGAR_RSS_URL.format(filing_type=urllib.parse.quote(filing_type, safe=""))
        items: list[NewsItem] = []
        try:
            await self._limiter.acquire()  # ≤10 req/s to EDGAR (task 1.3)
            raw_xml = await http.get_text(url)
            parsed = feedparser.parse(raw_xml)
            for entry in parsed.entries:
                title = getattr(entry, "title", "").strip() or f"SEC {filing_type}"
                link = getattr(entry, "link", "")
                pub_raw = getattr(entry, "published_parsed", None) or getattr(
                    entry, "updated_parsed", None
                )
                published_at = _parse_dt(pub_raw)

                # EDGAR Atom feeds bury structured data in <content>
                description = (
                    getattr(entry, "summary", None)
                    or getattr(entry, "description", None)
                    or ""
                ).strip()

                # Extract accession number from URL (…/Archives/edgar/data/<cik>/<acc>-index.htm)
                accession = ""
                if link:
                    parts = link.rstrip("/").split("/")
                    # accession number typically penultimate segment
                    for part in reversed(parts):
                        if part.count("-") == 2 and len(part) == 20:
                            accession = part
                            break

                item = NewsItem(
                    source=f"SEC EDGAR — {filing_type}",
                    source_type="sec",
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=link,
                    extra={
                        "filing_type": filing_type,
                        "accession_number": accession,
                        # Accession number is SEC's stable per-filing id (Gate 1 task 1.1).
                        "guid": getattr(entry, "id", "") or accession or link,
                    },
                )
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("SEC poll failed [%s]: %s", filing_type, exc)
        return items

    async def run(self) -> None:
        self._running = True
        log.info("SECExtractor started — types=%s, interval=%ss",
                 self.filing_types, self.poll_interval)
        async with _HttpClient(timeout=20) as http:
            while self._running:
                start = asyncio.get_running_loop().time()
                # Stagger requests to respect EDGAR rate limits
                for filing_type in self.filing_types:
                    if not self._running:
                        break
                    items = await self._poll_filing_type(filing_type, http)
                    for item in items:
                        await self.queue.put(item)
                    await asyncio.sleep(1.0)   # EDGAR courtesy delay
                elapsed = asyncio.get_running_loop().time() - start
                log.info("SECExtractor — cycle complete")
                await asyncio.sleep(max(0.0, self.poll_interval - elapsed))

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Stage 3 — FDA Extractor
# ---------------------------------------------------------------------------

_FDA_NEWS_RSS = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"
_FDA_MEDWATCH_RSS = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml"
#: openFDA recall/enforcement endpoints — same schema across product centers, so
#: one parser handles all three. Drug + device + food = the bulk of recall news.
_FDA_ENFORCEMENT_URLS: dict[str, str] = {
    "Drug": "https://api.fda.gov/drug/enforcement.json?sort=report_date:desc&limit=20",
    "Device": "https://api.fda.gov/device/enforcement.json?sort=report_date:desc&limit=20",
    "Food": "https://api.fda.gov/food/enforcement.json?sort=report_date:desc&limit=20",
}
_FDA_DRUG_EVENT_URL = (
    "https://api.fda.gov/drug/event.json"
    "?sort=receivedate:desc&limit=10"
)


class FDAExtractor:
    """
    Collects FDA news from several complementary primary sources:

    1. **FDA Press-Release RSS** — official announcements, drug approvals,
       safety communications.
    2. **FDA MedWatch RSS** — safety alerts / labeling changes (catalysts that
       don't always get a press release).
    3. **openFDA Enforcement** — recent recalls across the drug, device, and
       food centers (REST JSON, ``/{center}/enforcement.json``; shared schema).
    4. **openFDA Drug Adverse Events** — optional, high-volume (off by default).

    All are normalised into ``NewsItem`` objects (source_type="fda").
    """

    def __init__(
        self,
        poll_interval: float = 180.0,
        queue: asyncio.Queue | None = None,
        seen_cache: _SeenCache | None = None,
        include_drug_events: bool = False,  # high-volume; off by default
    ) -> None:
        self.poll_interval = poll_interval
        self.queue: asyncio.Queue = queue or asyncio.Queue()
        self._seen = seen_cache or _SeenCache()
        self.include_drug_events = include_drug_events
        self._running = False

    # ── RSS feeds (press releases + MedWatch safety alerts) ───────────── #

    async def _poll_rss(self, http: _HttpClient, url: str, source: str) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            raw_xml = await http.get_text(url)
            parsed = feedparser.parse(raw_xml)
            for entry in parsed.entries:
                title = getattr(entry, "title", "").strip() or source
                link = getattr(entry, "link", "")
                pub_raw = getattr(entry, "published_parsed", None) or getattr(
                    entry, "updated_parsed", None
                )
                published_at = _parse_dt(pub_raw)
                description = (
                    getattr(entry, "summary", "")
                    or getattr(entry, "description", "")
                ).strip()

                item = NewsItem(
                    source=source,
                    source_type="fda",
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=link,
                    extra={"guid": getattr(entry, "id", "") or link},  # task 1.1
                )
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("FDA RSS poll failed [%s]: %s", source, exc)
        return items

    # ── openFDA enforcement (REST JSON; drug / device / food share a schema) ─ #

    async def _poll_enforcement(
        self, http: _HttpClient, center: str, url: str
    ) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            data = await http.get_json(url)
            for result in data.get("results", []):
                product = result.get("product_description", "Unknown product")
                firm = result.get("recalling_firm", "Unknown firm")
                reason = result.get("reason_for_recall", "")
                recall_class = result.get("classification", "")
                status = result.get("status", "")
                report_date_raw = result.get("report_date", "")
                recall_number = result.get("recall_number", "")
                voluntary_mandated = result.get("voluntary_mandated", "")

                title = f"[{recall_class}] {firm} — {product[:80]}"
                description = (
                    f"Reason: {reason} | "
                    f"Status: {status} | "
                    f"Class: {recall_class} | "
                    f"Voluntary/Mandated: {voluntary_mandated}"
                )
                published_at = _parse_dt(report_date_raw)

                item = NewsItem(
                    source=f"FDA {center} Enforcement",
                    source_type="fda",
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=f"https://www.accessdata.fda.gov/scripts/enforcement/enforce_rpt-Product-Tabs.cfm?action=select&recall_number={recall_number}",
                    extra={
                        "recall_number": recall_number,
                        "classification": recall_class,
                        "status": status,
                        "recalling_firm": firm,
                        "center": center.lower(),
                        # recall_number is the stable id; the URL only templates it in.
                        "guid": recall_number,  # task 1.1
                    },
                )
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("FDA Enforcement poll failed [%s]: %s", center, exc)
        return items

    # ── openFDA adverse drug events (optional, high volume) ──────────── #

    async def _poll_drug_events(self, http: _HttpClient) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            data = await http.get_json(_FDA_DRUG_EVENT_URL)
            for result in data.get("results", []):
                receive_date = result.get("receivedate", "")
                report_id = result.get("safetyreportid", "")
                serious = result.get("serious", 1)
                reactions = [
                    r.get("reactionmeddrapt", "")
                    for r in result.get("patient", {}).get("reaction", [])[:5]
                ]
                drugs = [
                    d.get("medicinalproduct", "")
                    for d in result.get("patient", {}).get("drug", [])[:3]
                ]
                title = (
                    f"Adverse Event [{report_id}] — "
                    f"{', '.join(d for d in drugs if d)[:80]}"
                )
                description = (
                    f"Reactions: {', '.join(r for r in reactions if r)} | "
                    f"Serious: {'Yes' if serious else 'No'}"
                )
                published_at = _parse_dt(receive_date)

                item = NewsItem(
                    source="FDA Adverse Events",
                    source_type="fda",
                    title=title,
                    published_at=published_at,
                    description=description,
                    url=f"https://www.accessdata.fda.gov/scripts/cder/daf/",
                    extra={
                        "report_id": report_id,
                        "serious": bool(serious),
                        "drugs": drugs,
                        "reactions": reactions,
                        # All drug-event items share one templated URL, so the
                        # safetyreportid is the ONLY unique id here (task 1.1).
                        "guid": report_id,
                    },
                )
                if self._seen.is_new(item):
                    items.append(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("FDA Drug Events poll failed: %s", exc)
        return items

    # ── Lifecycle ────────────────────────────────────────────────────── #

    async def run(self) -> None:
        self._running = True
        log.info("FDAExtractor started — interval=%ss", self.poll_interval)
        async with _HttpClient(timeout=20) as http:
            while self._running:
                start = asyncio.get_running_loop().time()
                batches = await asyncio.gather(
                    self._poll_rss(http, _FDA_NEWS_RSS, "FDA Press Releases"),
                    self._poll_rss(http, _FDA_MEDWATCH_RSS, "FDA MedWatch Safety Alerts"),
                    *(
                        self._poll_enforcement(http, center, url)
                        for center, url in _FDA_ENFORCEMENT_URLS.items()
                    ),
                    *(
                        [self._poll_drug_events(http)]
                        if self.include_drug_events
                        else []
                    ),
                    return_exceptions=True,
                )
                total = 0
                for batch in batches:
                    if isinstance(batch, list):
                        for item in batch:
                            await self.queue.put(item)
                            total += 1
                log.info("FDAExtractor — cycle complete, %d new items", total)
                elapsed = asyncio.get_running_loop().time() - start
                await asyncio.sleep(max(0.0, self.poll_interval - elapsed))

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Ingestion Agent — top-level orchestrator
# ---------------------------------------------------------------------------

class IngestionAgent:
    """
    Orchestrates all extractors and routes items to registered handlers.

    Parameters
    ----------
    rss_feeds:
        Override the default RSS feed list (see ``DEFAULT_RSS_FEEDS``).
    rss_poll_interval:
        Seconds between RSS polling cycles. Default 60 s.
    sec_filing_types:
        Filing types to watch on EDGAR. Default: 8-K, 10-K, 10-Q, S-1, 6-K.
    sec_poll_interval:
        Seconds between SEC EDGAR cycles. Default 300 s.
    fda_poll_interval:
        Seconds between FDA cycles. Default 180 s.
    fda_include_drug_events:
        Enable high-volume adverse-event polling. Default False.
    queue_maxsize:
        Maximum items buffered in the internal queue. 0 = unbounded.

    Example
    -------
    ::

        agent = IngestionAgent(rss_poll_interval=30)
        agent.dispatcher.register(my_async_handler)
        asyncio.run(agent.run())
    """

    def __init__(
        self,
        rss_feeds: list[dict[str, str]] | None = None,
        rss_poll_interval: float = 60.0,
        sec_filing_types: list[str] | None = None,
        sec_poll_interval: float = 300.0,
        fda_poll_interval: float = 180.0,
        fda_include_drug_events: bool = False,
        queue_maxsize: int = 0,
        enable_rss: bool = True,
        enable_sec: bool = True,
        enable_fda: bool = True,
        keywords: list[str] | None = None,
        rss_max_age_minutes: float | None = None,
        reddit_per_cycle: int = 1,
    ) -> None:
        self._queue: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=queue_maxsize)
        self._seen = _SeenCache()
        self.dispatcher = DispatchRouter()
        # Use FILTER_KEYWORDS by default; pass keywords=[] to disable filtering
        self._filter = KeywordFilter(keywords if keywords is not None else FILTER_KEYWORDS)
        # Feeds that declare always_dispatch skip keyword gating (exchange-ops
        # rows like Nasdaq Trade Halts have bare-symbol titles by design).
        self._filter_exempt: frozenset = frozenset(
            f.get("label", "") for f in (rss_feeds or DEFAULT_RSS_FEEDS)
            if f.get("always_dispatch")
        )
        self._classifier = TopicClassifier()
        from ticker_extractor import TickerExtractor
        self._ticker_extractor = TickerExtractor()

        self.enable_rss = enable_rss
        self.enable_sec = enable_sec
        self.enable_fda = enable_fda

        self.rss = RSSExtractor(
            feeds=rss_feeds,
            poll_interval=rss_poll_interval,
            queue=self._queue,
            seen_cache=self._seen,
            max_age_minutes=rss_max_age_minutes,
            reddit_per_cycle=reddit_per_cycle,
        ) if enable_rss else None
        self.sec = SECExtractor(
            filing_types=sec_filing_types,
            poll_interval=sec_poll_interval,
            queue=self._queue,
            seen_cache=self._seen,
        ) if enable_sec else None
        self.fda = FDAExtractor(
            poll_interval=fda_poll_interval,
            queue=self._queue,
            seen_cache=self._seen,
            include_drug_events=fda_include_drug_events,
        ) if enable_fda else None

        self._tasks: list[asyncio.Task] = []
        self._dispatch_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    #  Dispatch loop                                                       #
    # ------------------------------------------------------------------ #

    async def _dispatch_loop(self) -> None:
        """Drain the shared queue, apply keyword filter, classify topic, and fan-out to handlers."""
        while True:
            item = await self._queue.get()
            # SEC and FDA items are inherently financial — skip keyword gating so
            # no filing or enforcement notice is silently dropped just because
            # "Apple" or "Pfizer" don't appear in FILTER_KEYWORDS.
            is_regulatory = item.source_type in ("sec", "fda")
            is_exempt_feed = item.source in self._filter_exempt
            if is_regulatory or is_exempt_feed or self._filter.accepts(item):
                # Only social items (Reddit RSS feeds carry source_type="social")
                # get their cashtags gated against the real-ticker universe —
                # that's where fake $YOLO/$MOON symbols come from. Structured
                # RSS/SEC/FDA keep un-gated extraction.
                item = replace(
                    item,
                    topic=self._classifier.classify(item),
                    tickers=self._ticker_extractor.extract(
                        item.title,
                        item.description,
                        validate=(item.source_type == "social"),
                    ),
                )
                await self.dispatcher.dispatch(item)
            else:
                log.debug("Filtered out: [%s] %s", item.source_type, item.title[:80])
            self._queue.task_done()

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def _install_ticker_universe(self) -> None:
        """
        Load the real-ticker universe (all US-listed symbols + SEC + major crypto)
        and install it on the extractor so social cashtags can be validated against
        it. Best effort: on a fetch failure the universe stays unset and social
        extraction falls back to un-gated behavior rather than dropping every ticker.
        """
        try:
            from listed_symbols import load_valid_tickers
            universe = await load_valid_tickers()
            if universe:
                self._ticker_extractor.set_valid_tickers(universe)
                log.info(
                    "social ticker validation ON — %d real symbols in universe",
                    len(universe),
                )
            else:
                log.warning(
                    "ticker universe empty — social cashtag validation disabled this run"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ticker universe load failed (%s) — social validation disabled",
                type(exc).__name__,
            )

    async def start(self) -> None:
        """Launch enabled extractors and the dispatch loop as background tasks."""
        await self._install_ticker_universe()
        loop = asyncio.get_running_loop()
        self._tasks = []
        if self.rss is not None:
            self._tasks.append(loop.create_task(self.rss.run(), name="rss_extractor"))
        if self.sec is not None:
            self._tasks.append(loop.create_task(self.sec.run(), name="sec_extractor"))
        if self.fda is not None:
            self._tasks.append(loop.create_task(self.fda.run(), name="fda_extractor"))
        self._dispatch_task = loop.create_task(
            self._dispatch_loop(), name="dispatch_loop"
        )
        enabled = [
            s for s, on in (("rss", self.enable_rss), ("sec", self.enable_sec), ("fda", self.enable_fda)) if on
        ]
        log.info("IngestionAgent started — sources: %s (%d tasks)", enabled, len(self._tasks))

    async def stop(self) -> None:
        """Gracefully stop all enabled extractors and flush remaining items."""
        if self.rss is not None:
            self.rss.stop()
        if self.sec is not None:
            self.sec.stop()
        if self.fda is not None:
            self.fda.stop()
        for task in self._tasks:
            task.cancel()
        if self._dispatch_task:
            self._dispatch_task.cancel()
        await asyncio.gather(*self._tasks, self._dispatch_task or asyncio.sleep(0), return_exceptions=True)
        log.info("IngestionAgent stopped")

    async def run(self) -> None:
        """
        Start the agent and block until interrupted.

        Catches ``KeyboardInterrupt`` / ``CancelledError`` and shuts down
        cleanly.
        """
        await self.start()
        try:
            # Include the dispatch task so all long-running tasks are awaited
            # together; any one finishing (or raising) triggers the finally block.
            all_tasks = [
                *self._tasks,
                *([self._dispatch_task] if self._dispatch_task else []),
            ]
            await asyncio.gather(*all_tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Shutdown signal received")
        finally:
            await self.stop()

    # ------------------------------------------------------------------ #
    #  Status                                                              #
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        """Return a dict summarising the agent's current state."""
        def task_state(t: asyncio.Task | None) -> str:
            if t is None:
                return "not_started"
            if t.done():
                return "done" if not t.cancelled() else "cancelled"
            return "running"

        def source_state(name: str, enabled: bool) -> str:
            if not enabled:
                return "disabled"
            return task_state(next((t for t in self._tasks if t.get_name() == name), None))

        return {
            "queue_size": self._queue.qsize(),
            "seen_cache_size": len(self._seen._cache),
            "rss_task": source_state("rss_extractor", self.enable_rss),
            "sec_task": source_state("sec_extractor", self.enable_sec),
            "fda_task": source_state("fda_extractor", self.enable_fda),
            "dispatch_task": task_state(self._dispatch_task),
            "registered_handlers": len(self.dispatcher._handlers),
        }


# ---------------------------------------------------------------------------
# CSV export handler
# ---------------------------------------------------------------------------

# Column order written to every CSV file produced by CSVHandler
_CSV_COLUMNS = ["source", "source_type", "title", "published_at", "url", "description", "extra"]


class CSVHandler:
    """
    Async-compatible handler that appends each dispatched NewsItem as a row
    in a CSV file.

    Parameters
    ----------
    path:
        Destination file path.  Created on first write; appended to on restart.
    enabled:
        Set to False to make the handler a no-op without unregistering it.
        Useful for toggling CSV output without restarting the agent.

    Usage::

        handler = CSVHandler("output.csv", enabled=True)
        agent.dispatcher.register(handler)
    """

    def __init__(self, path: str, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        # Write the header only when creating a brand-new file
        self._write_header = not os.path.exists(path)

    async def __call__(self, item: NewsItem) -> None:
        # Short-circuit immediately when disabled — zero overhead
        if not self.enabled:
            return

        # File I/O is synchronous but fast for single-row appends; wrapping in
        # an executor would add overhead that isn't justified at this volume.
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
            if self._write_header:
                writer.writeheader()
                self._write_header = False
            writer.writerow({
                "source":       item.source,
                "source_type":  item.source_type,
                "title":        item.title,
                "published_at": item.published_at.isoformat(),
                "url":          item.url,
                "description":  item.description,
                "extra":        json.dumps(item.extra) if item.extra else "",
            })


