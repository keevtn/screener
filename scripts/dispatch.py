"""Manual single-source dispatch + scheduler entry point (docs/ROADMAP.md task 1.2).

``run_source_once(source, sink)`` polls one source exactly once (driving the
backend extractors' `_poll_*` helpers) and writes results to the raw_items sink.
Both the manual CLI (``--source X``) and the APScheduler jobs call this same
function, so a scheduled cycle and a manual dispatch produce identical rows.

Usage:
    python scripts/dispatch.py --source sec [--url DATABASE_URL]
    python scripts/dispatch.py --schedule            # run the scheduler loop
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

log = logging.getLogger("dispatch")

_REPO = Path(__file__).resolve().parents[1]

# Load .env so EDGAR_USER_AGENT / DATABASE_URL are available for the soak run.
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
except ImportError:
    pass

# The backend extractors use flat imports and expect backend/ on sys.path.
_BACKEND = _REPO / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from pipeline.common.db import make_engine  # noqa: E402
from pipeline.ingest import RawItemHandler  # noqa: E402
from pipeline.ingest.scheduler import DEFAULT_CADENCES, build_scheduler  # noqa: E402

SOURCES = ("rss", "reddit", "sec", "fda", "bluesky")


async def _drive(source, sink, http, M, feeds, filing_types) -> int:
    items = []
    if source == "bluesky":
        # One-shot sweep over the Bluesky search terms (public AppView, no auth).
        # Social class -> shadow-mode raw_items archival only (I8, never scored);
        # ticker attribution happens in enrichment via cashtags in the text.
        import UnstructuredModule as U

        ext = U.BlueskyExtractor()
        terms = feeds or ext.search_terms  # `feeds` doubles as the terms override (tests)
        for term in terms:
            # Per-term fail-soft, mirroring the extractor's degrade-gracefully
            # contract: a transient 403/429 on one term (observed live: burst
            # rate-limits) must not lose the other terms' items.
            try:
                data = await http.get_json(
                    ext._SEARCH_URL, params={"q": term, "limit": ext._RESULTS_PER_TERM}
                )
            except Exception:  # noqa: BLE001 — non-200 -> empty cycle for this term
                data = None
            items += ext.parse_posts(data or {}, term)
            # ~1s pacing x ~26 terms per 5-min full sweep stays under the
            # continuous extractor's own request rate (2s pace / 300s interval).
            await asyncio.sleep(1.0)
        return sum(sink.write(item) for item in items)
    if source == "reddit":
        # OAuth when an (approved) app's creds are set; else the residential RSS
        # fallback that works today (JSON API is 403; new OAuth apps are approval-
        # gated). Both social -> shadow-mode raw_items, both fail-soft.
        from pipeline.ingest.reddit import (
            RedditHttp,
            RedditOAuthExtractor,
            RedditRSSExtractor,
            RedditRSSHttp,
            reddit_credentials,
        )

        creds = reddit_credentials()
        if creds is not None:
            ext = RedditOAuthExtractor(creds, groups=feeds)  # feeds = groups override (tests)
            if hasattr(http, "post_form"):  # test-injected reddit-shaped client
                items = await ext.poll(http)
            else:
                import aiohttp

                async with aiohttp.ClientSession() as s:
                    items = await ext.poll(RedditHttp(s))
        else:
            rss = RedditRSSExtractor(groups=feeds)  # one group per sweep (rotates)
            if hasattr(http, "fetch"):  # test-injected RSS client (NOT the backend _HttpClient)
                items = await rss.poll(http)
            else:
                import aiohttp

                async with aiohttp.ClientSession() as s:
                    items = await rss.poll(RedditRSSHttp(s))
        return sum(sink.write(item) for item in items)
    if source == "rss":
        ext = M.RSSExtractor(feeds=feeds)
        for cfg in ext.feeds:
            if cfg.get("source_type") != "social":  # structured RSS only; reddit is OAuth now
                items += await ext._poll_feed(cfg, http)
    elif source == "sec":
        ext = M.SECExtractor(filing_types=filing_types)
        for filing_type in ext.filing_types:
            items += await ext._poll_filing_type(filing_type, http)
    elif source == "fda":
        ext = M.FDAExtractor()
        items += await ext._poll_rss(http, M._FDA_NEWS_RSS, "FDA Press Releases")
        items += await ext._poll_rss(http, M._FDA_MEDWATCH_RSS, "FDA MedWatch Safety Alerts")
        for center, url in M._FDA_ENFORCEMENT_URLS.items():
            items += await ext._poll_enforcement(http, center, url)
    else:
        raise ValueError(f"unknown source {source!r}")
    return sum(sink.write(item) for item in items)


async def run_source_once(
    source: str,
    sink: RawItemHandler,
    *,
    http=None,
    feeds=None,
    filing_types=None,
) -> int:
    """Poll one source once into ``sink``; return the count of new rows written.

    ``http`` may be an already-open client (tests inject a fake); otherwise a real
    backend ``_HttpClient`` is opened for the call.
    """
    import IngestionModule as M

    if http is not None:
        return await _drive(source, sink, http, M, feeds, filing_types)
    async with M._HttpClient() as client:
        return await _drive(source, sink, client, M, feeds, filing_types)


async def _run_scheduler(sink: RawItemHandler) -> None:
    async def dispatch_fn(source: str) -> int:
        n = await run_source_once(source, sink)
        print(f"[scheduler] {source}: {n} new rows")
        return n

    sched, _ = build_scheduler(dispatch_fn)
    sched.start()
    print(f"scheduler started — cadences(s): {DEFAULT_CADENCES}")
    try:
        await asyncio.Event().wait()  # run until cancelled
    except (KeyboardInterrupt, asyncio.CancelledError):
        sched.shutdown(wait=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=SOURCES, help="poll one source once and exit")
    parser.add_argument("--schedule", action="store_true", help="run the APScheduler loop")
    parser.add_argument("--url", default=None, help="database URL (default: $DATABASE_URL)")
    args = parser.parse_args()

    sink = RawItemHandler(make_engine(args.url))
    if args.schedule:
        asyncio.run(_run_scheduler(sink))
    elif args.source:
        n = asyncio.run(run_source_once(args.source, sink))
        print(f"{args.source}: {n} new rows")
    else:
        parser.error("pass --source X or --schedule")


if __name__ == "__main__":
    main()
