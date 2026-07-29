"""
UnstructuredModule.py
=====================
Social media ingestion for the financial news dashboard.

Sources
-------
StockTwits  — unauthenticated public symbol stream API (200 req/hour limit).
              Endpoint: https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json
              With 22 default tickers, one full cycle uses 22 requests.
              Default poll_interval=480 s (8 min) keeps well under the ceiling.

Bluesky     — AT Protocol public AppView endpoint, no credentials required.
              Endpoint: https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts
              Rate limits are not formally documented; we pace with a 2 s
              inter-request sleep and back off 60 s on HTTP 429.

Both sources emit NewsItem objects with source_type="social" and feed the
same dispatcher → MongoDB pipeline as the structured ingestion sources
(IngestionModule.py).  The TopicClassifier and KeywordFilter from
IngestionModule are reused so both pipelines stay in sync.

Architecture
------------
  StockTwitsExtractor ─┐
                        ├─ asyncio.Queue ─► UnstructuredAgent._dispatch_loop
  BlueskyExtractor   ──┘                        │
                                                 ├─ KeywordFilter
                                                 ├─ TopicClassifier
                                                 └─ DispatchRouter (shared)

Usage
-----
    import asyncio
    from UnstructuredModule import UnstructuredAgent
    from IngestionModule import DispatchRouter

    router = DispatchRouter()
    router.register(my_handler)

    agent = UnstructuredAgent(dispatcher=router)
    asyncio.run(agent.run())          # blocks until Ctrl-C

    # — or non-blocking —
    await agent.start()
    # ... other work ...
    await agent.stop()

CLI (standalone test)
---------------------
    python UnstructuredModule.py --stocktwits --bluesky

Dependencies
------------
    pip install aiohttp python-dateutil
    (both already in backend/requirements.txt)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import aiohttp
from dateutil import parser as dateutil_parser

from IngestionModule import (
    NewsItem,
    DispatchRouter,
    KeywordFilter,
    TopicClassifier,
    FILTER_KEYWORDS,
    STOCKTWITS_WATCHLIST,
    BLUESKY_SEARCH_TERMS,
    _SeenCache,  # same dedup cache used by structured sources
)
from social_filter import is_nsfw_post

log = logging.getLogger("unstructured_agent")

# ---------------------------------------------------------------------------
# Shared HTTP headers
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": "FinancialNewsDashboard/1.0 (research/non-commercial)",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_dt_str(raw: str | None) -> datetime:
    """Parse an ISO 8601 string into a UTC-aware datetime; fall back to now."""
    if not raw:
        return datetime.now(tz=timezone.utc)
    try:
        dt = dateutil_parser.parse(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Stage A — StockTwits Extractor
# ---------------------------------------------------------------------------

class StockTwitsExtractor:
    """
    Polls the StockTwits public unauthenticated symbol stream for each ticker
    in STOCKTWITS_WATCHLIST.

    Notable extras stored in NewsItem.extra:
      st_sentiment  — user-tagged "Bullish" / "Bearish" / None (human labels,
                      valuable for cross-validating our FinBERT scores)
      ticker        — the symbol being watched
      symbols       — all tickers mentioned in the message body
      st_user       — StockTwits username of the author
    """

    _STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"

    def __init__(
        self,
        watchlist: list[str] | None = None,
        poll_interval: float = 480.0,   # 8 min default — safe under 200 req/hour
        queue: asyncio.Queue | None = None,
        seen_cache: _SeenCache | None = None,
    ) -> None:
        self.watchlist = watchlist or STOCKTWITS_WATCHLIST
        self.poll_interval = poll_interval
        self.queue: asyncio.Queue = queue or asyncio.Queue()
        self._seen = seen_cache or _SeenCache()
        self._running = False

    async def _poll_ticker(
        self, ticker: str, session: aiohttp.ClientSession
    ) -> list[NewsItem]:
        url = self._STREAM_URL.format(ticker=ticker)
        items: list[NewsItem] = []
        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    log.warning("StockTwits rate-limited — backing off 60 s")
                    await asyncio.sleep(60)
                    return []
                if resp.status != 200:
                    log.debug("StockTwits [%s] HTTP %d", ticker, resp.status)
                    return []
                data: dict[str, Any] = await resp.json(content_type=None)

            for msg in data.get("messages", []):
                body: str = msg.get("body", "").strip()
                if not body:
                    continue

                msg_id = msg.get("id", "")
                username: str = (msg.get("user") or {}).get("username", "unknown")
                published_at = _parse_dt_str(msg.get("created_at"))

                # StockTwits sends ``entities``/``sentiment`` as JSON null on many
                # messages, so ``.get(k, {})`` returns None (the key exists) and
                # chaining .get() crashes. Coalesce each level with ``or {}``.
                entities = msg.get("entities") or {}
                sentiment = entities.get("sentiment") or {}
                st_sentiment = sentiment.get("basic")  # "Bullish" / "Bearish" / None
                symbols = [
                    s["symbol"]
                    for s in (entities.get("symbols") or [])
                    if s.get("symbol")
                ]

                item = NewsItem(
                    source=f"StockTwits — ${ticker}",
                    source_type="social",
                    title=body[:120] + ("…" if len(body) > 120 else ""),
                    published_at=published_at,
                    description=body,
                    url=f"https://stocktwits.com/{username}/message/{msg_id}",
                    extra={
                        "st_sentiment": st_sentiment,
                        "ticker": ticker,
                        "symbols": symbols,
                        "st_user": username,
                    },
                )
                if self._seen.is_new(item):
                    items.append(item)

        except Exception as exc:  # noqa: BLE001
            log.warning("StockTwits poll failed [%s]: %s", ticker, exc)

        return items

    async def run(self) -> None:
        self._running = True
        # NOTE: StockTwits fronts this API with Cloudflare, which blocks plain
        # aiohttp by TLS fingerprint (403). We deliberately do NOT bypass that
        # with browser impersonation — that would be circumventing their access
        # control. So this extractor is disabled by default; if enabled it makes
        # ordinary requests and simply gets blocked (no data, no crash). Kept for
        # reference / in case StockTwits ever opens a sanctioned path again.
        log.info(
            "StockTwitsExtractor started — %d tickers, interval=%ss",
            len(self.watchlist), self.poll_interval,
        )
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(headers=_HEADERS, timeout=timeout) as session:
            while self._running:
                loop_start = asyncio.get_running_loop().time()
                total = 0
                for ticker in self.watchlist:
                    for item in await self._poll_ticker(ticker, session):
                        await self.queue.put(item)
                        total += 1
                    # 1.5 s between tickers avoids burst-hitting the rate limit
                    await asyncio.sleep(1.5)
                log.info("StockTwitsExtractor — cycle done, %d new items", total)
                elapsed = asyncio.get_running_loop().time() - loop_start
                await asyncio.sleep(max(0.0, self.poll_interval - elapsed))

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Stage B — Bluesky Extractor
# ---------------------------------------------------------------------------

class BlueskyExtractor:
    """
    Searches the Bluesky public AT Protocol API for financial posts matching
    BLUESKY_SEARCH_TERMS.

    No API key, no registration, no cost. Uses the AppView host ``api.bsky.app``
    which serves ``searchPosts`` unauthenticated.

    NOTE: as of mid-2026 the *public* AppView host (``public.api.bsky.app``)
    started returning ``403 Forbidden`` for ``app.bsky.feed.searchPosts`` (an
    anti-scraping change) while still serving other endpoints. The main AppView
    host ``api.bsky.app`` continues to serve the same search unauthenticated, so
    we point there. If that is ever locked down too, the extractor degrades
    gracefully (non-200 → empty cycle, logged) — no crash.

    Each search term gets 25 results per cycle; a 2 s sleep between requests
    keeps traffic polite.  A 60 s back-off fires on HTTP 429.

    Extras stored in NewsItem.extra:
      bsky_handle   — author's Bluesky handle
      bsky_uri      — AT URI (at://did/…/rkey) for programmatic reference
      likes         — like count at time of ingestion
      replies       — reply count at time of ingestion
      search_term   — which search term surfaced this post
    """

    _SEARCH_URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
    _RESULTS_PER_TERM = 25  # max per request; API supports up to 100

    def __init__(
        self,
        search_terms: list[str] | None = None,
        poll_interval: float = 300.0,  # 5 min default
        queue: asyncio.Queue | None = None,
        seen_cache: _SeenCache | None = None,
    ) -> None:
        self.search_terms = search_terms or BLUESKY_SEARCH_TERMS
        self.poll_interval = poll_interval
        self.queue: asyncio.Queue = queue or asyncio.Queue()
        self._seen = seen_cache or _SeenCache()
        self._running = False

    @staticmethod
    def _at_uri_to_web_url(uri: str, handle: str) -> str:
        """Convert at://did/.../rkey to https://bsky.app/profile/{handle}/post/{rkey}."""
        try:
            rkey = uri.rstrip("/").rsplit("/", 1)[-1]
            return f"https://bsky.app/profile/{handle}/post/{rkey}"
        except Exception:
            return ""

    @classmethod
    def parse_posts(cls, data: dict[str, Any], term: str) -> list[NewsItem]:
        """searchPosts response -> NewsItems (NSFW/empty dropped). Pure parser —
        shared by the continuous middleware loop and the one-shot raw_items
        dispatch path (scripts/dispatch.py --source bluesky)."""
        items: list[NewsItem] = []
        for post in (data or {}).get("posts", []):
            if is_nsfw_post(post):   # drop adult/spam (labelled or blocklisted)
                continue
            record = post.get("record", {})
            author = post.get("author", {})
            text: str = record.get("text", "").strip()
            if not text:
                continue

            handle: str = author.get("handle", "unknown.bsky.social")
            published_at = _parse_dt_str(record.get("createdAt"))
            uri: str = post.get("uri", "")

            items.append(
                NewsItem(
                    source="Bluesky",
                    source_type="social",
                    title=text[:120] + ("…" if len(text) > 120 else ""),
                    published_at=published_at,
                    description=text,
                    url=cls._at_uri_to_web_url(uri, handle),
                    extra={
                        "bsky_handle": handle,
                        "bsky_uri": uri,
                        "likes": post.get("likeCount") or 0,
                        "replies": post.get("replyCount") or 0,
                        "search_term": term,
                    },
                )
            )
        return items

    async def _search_term(
        self, term: str, session: aiohttp.ClientSession
    ) -> list[NewsItem]:
        try:
            params = {"q": term, "limit": self._RESULTS_PER_TERM}
            async with session.get(self._SEARCH_URL, params=params) as resp:
                if resp.status == 429:
                    log.warning("Bluesky rate-limited — backing off 60 s")
                    await asyncio.sleep(60)
                    return []
                if resp.status != 200:
                    log.debug("Bluesky search '%s' HTTP %d", term, resp.status)
                    return []
                data: dict[str, Any] = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("Bluesky search failed ['%s']: %s", term, exc)
            return []
        return [it for it in self.parse_posts(data, term) if self._seen.is_new(it)]

    async def run(self) -> None:
        self._running = True
        log.info(
            "BlueskyExtractor started — %d search terms, interval=%ss",
            len(self.search_terms), self.poll_interval,
        )
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(headers=_HEADERS, timeout=timeout) as session:
            while self._running:
                loop_start = asyncio.get_running_loop().time()
                total = 0
                for term in self.search_terms:
                    for item in await self._search_term(term, session):
                        await self.queue.put(item)
                        total += 1
                    await asyncio.sleep(2.0)  # polite pacing between search requests
                log.info("BlueskyExtractor — cycle done, %d new items", total)
                elapsed = asyncio.get_running_loop().time() - loop_start
                await asyncio.sleep(max(0.0, self.poll_interval - elapsed))

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# UnstructuredAgent — orchestrates both extractors
# ---------------------------------------------------------------------------

class UnstructuredAgent:
    """
    Runs StockTwitsExtractor and BlueskyExtractor concurrently, routing all
    social posts through the shared DispatchRouter (KeywordFilter → TopicClassifier
    → registered handlers).

    Designed to share a DispatchRouter with IngestionAgent so both structured
    and social sources write to the same MongoDB collection in one process.

    Parameters
    ----------
    dispatcher:
        DispatchRouter with handlers already registered (e.g. MongoHandler).
    keywords:
        Override FILTER_KEYWORDS. Pass [] to disable filtering entirely.
    enable_stocktwits / enable_bluesky:
        Toggle individual sources at construction time.
    stocktwits_interval / bluesky_interval:
        Override default poll intervals (seconds).
    """

    def __init__(
        self,
        dispatcher: DispatchRouter | None = None,
        keywords: list[str] | None = None,
        enable_stocktwits: bool = False,  # off: clean (non-impersonating) access is Cloudflare-blocked
        enable_bluesky: bool = True,
        stocktwits_interval: float = 480.0,
        bluesky_interval: float = 300.0,
    ) -> None:
        self.dispatcher = dispatcher or DispatchRouter()

        # Shared queue and seen cache — both extractors write here
        self._queue: asyncio.Queue = asyncio.Queue()
        self._seen = _SeenCache()

        self._filter = KeywordFilter(
            keywords if keywords is not None else FILTER_KEYWORDS
        )
        self._classifier = TopicClassifier()
        try:
            from ticker_extractor import TickerExtractor, extract_social_tickers
            self._ticker_extractor: "TickerExtractor | None" = TickerExtractor()
            self._extract_social = extract_social_tickers
        except ImportError:
            log.warning("ticker_extractor not found — social tickers won't be tagged")
            self._ticker_extractor = None
            self._extract_social = None

        self.enable_stocktwits = enable_stocktwits
        self.enable_bluesky = enable_bluesky

        self._stocktwits = StockTwitsExtractor(
            poll_interval=stocktwits_interval,
            queue=self._queue,
            seen_cache=self._seen,
        )
        self._bluesky = BlueskyExtractor(
            poll_interval=bluesky_interval,
            queue=self._queue,
            seen_cache=self._seen,
        )
        self._tasks: list[asyncio.Task] = []

    def _extract_tickers(self, item: NewsItem) -> tuple[str, ...]:
        """
        Combine platform-provided tickers with validated TickerExtractor results.
        StockTwits items carry API-resolved symbols in extra["ticker"]/extra["symbols"];
        for Reddit and Bluesky, TickerExtractor handles $TICKER patterns and company
        names. Cashtags and platform symbols are validated against the real-ticker
        universe (SEC + major crypto) so fake $YOLO/$MOON tags never reach the feed.
        """
        if self._ticker_extractor is None or self._extract_social is None:
            # extractor unavailable — fall back to raw platform symbols only
            found: set[str] = set()
            wl_ticker = item.extra.get("ticker")
            if wl_ticker:
                found.add(str(wl_ticker).replace(".X", ""))
            for sym in item.extra.get("symbols", []) or []:
                if sym:
                    found.add(str(sym).replace(".X", ""))
            return tuple(sorted(found))

        return self._extract_social(
            self._ticker_extractor, item.title, item.description, item.extra
        )

    async def _dispatch_loop(self) -> None:
        """Drain the shared queue: filter → classify → extract tickers → dispatch."""
        while True:
            item = await self._queue.get()
            try:
                if self._filter.accepts(item):
                    item = replace(
                        item,
                        topic=self._classifier.classify(item),
                        tickers=self._extract_tickers(item),
                    )
                    await self.dispatcher.dispatch(item)
                else:
                    log.debug(
                        "Social item filtered: [%s] %s",
                        item.source, item.title[:60],
                    )
            finally:
                self._queue.task_done()

    async def _install_ticker_universe(self) -> None:
        """
        Load SEC's real-ticker universe (+ major crypto) and install it on the
        extractor so social cashtags ($YOLO, $MOON …) are validated away. Best
        effort: on a fetch failure the universe stays unset and extraction falls
        back to un-gated behavior rather than dropping every ticker.
        """
        if self._ticker_extractor is None:
            return
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
        """Start all enabled extractors and the dispatch loop as background tasks."""
        await self._install_ticker_universe()
        self._tasks = [
            asyncio.create_task(self._dispatch_loop(), name="social-dispatch")
        ]
        if self.enable_stocktwits:
            self._tasks.append(
                asyncio.create_task(self._stocktwits.run(), name="stocktwits")
            )
        if self.enable_bluesky:
            self._tasks.append(
                asyncio.create_task(self._bluesky.run(), name="bluesky")
            )
        log.info(
            "UnstructuredAgent started — stocktwits=%s  bluesky=%s",
            self.enable_stocktwits, self.enable_bluesky,
        )

    async def stop(self) -> None:
        self._stocktwits.stop()
        self._bluesky.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("UnstructuredAgent stopped")

    async def run(self) -> None:
        """Block until Ctrl-C. Convenient for standalone use via run_ingest.py."""
        await self.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.stop()


# ---------------------------------------------------------------------------
# Standalone entry point (quick smoke-test without the full ingestion stack)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    ap = argparse.ArgumentParser(description="Test UnstructuredModule extractors")
    ap.add_argument("--stocktwits", action="store_true", help="Enable StockTwits")
    ap.add_argument("--bluesky",    action="store_true", help="Enable Bluesky")
    args = ap.parse_args()

    any_on = args.stocktwits or args.bluesky
    use_st = args.stocktwits if any_on else True
    use_bk = args.bluesky    if any_on else True

    async def _print_handler(item: NewsItem) -> None:
        print(item)
        print("-" * 80)

    async def _run() -> None:
        router = DispatchRouter()
        router.register(_print_handler)
        agent = UnstructuredAgent(
            dispatcher=router,
            keywords=[],          # no filtering in smoke-test
            enable_stocktwits=use_st,
            enable_bluesky=use_bk,
        )
        await agent.run()

    asyncio.run(_run())
