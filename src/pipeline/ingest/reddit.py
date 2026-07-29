"""Reddit lane — residential RSS by default, official OAuth as an upgrade.

ACCESS PATH (verified live from this box, 2026-07-19):
  * The unauthenticated JSON API (``/r/…/.json``, old.reddit) is 403 EVEN from a
    residential IP now — Reddit broadly gated unauthenticated JSON.
  * The Atom RSS feed (``/r/…/new/.rss``) DOES serve from residential, but with a
    harsh per-IP rate limit: ~one request per cooldown window (a 4-group burst at
    12s spacing yielded 1x200 then 3x429, recovering after ~40s idle).
  * New OAuth apps are now behind Reddit's manual "Responsible Builder" approval
    (can take days, may fail) — a real bottleneck, not instant self-serve.

So the DEFAULT is the residential RSS fallback, polling ONE subreddit group per
sweep (rotating) to stay under the RSS rate limit — works today, no creds, no
proxy. The OAuth path (100 QPM, all groups per sweep) activates automatically
once REDDIT_CLIENT_ID/SECRET are set (i.e. after an app is approved). Both are
env/degradation fail-soft. Social lane: rows land in raw_items shadow-mode (I8),
ticker attribution via cashtags happens later in enrichment.

OAuth credential setup (relay to the operator — only needed for the upgrade):
  1. Sign in at https://www.reddit.com/prefs/apps  (a real Reddit account)
  2. "create another app…" -> type **web app** (confidential; app-only tokens).
     Name it anything; redirect uri http://localhost:8080 (unused for app-only).
  3. NOTE: since late 2025 new API access routes through Reddit's approval form
     (Responsible Builder Policy) — approval can take days. Until then the RSS
     fallback keeps the lane live, so this is a no-rush upgrade.
  4. Copy: the id UNDER the app name -> REDDIT_CLIENT_ID; "secret" -> REDDIT_CLIENT_SECRET.
  5. Set REDDIT_USER_AGENT="financial-news-screener/0.1 by /u/<your_username>".
  6. Put all three in .env. Read-only public listings need no user password.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

# The finance subreddit groups (mirror the RSS lane's set); polled multi-sub in
# one request each (r/a+b+c/new) to stay well inside Reddit's rate budget.
REDDIT_GROUPS: list[list[str]] = [
    ["wallstreetbets", "SecurityAnalysis", "ValueInvesting", "dividends"],
    ["stocks", "economics", "algotrading", "thetagang"],
    ["options", "econmonitor", "Daytrading", "Shortsqueeze"],
    ["CryptoCurrency", "Bitcoin", "investing", "StockMarket", "pennystocks"],
]

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
_DEFAULT_UA = "financial-news-screener/0.1 (app-only)"

log = logging.getLogger("pipeline.ingest.reddit")


def reddit_credentials() -> tuple[str, str, str] | None:
    """(client_id, client_secret, user_agent) from the env, or None if unset."""
    cid = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        return None
    ua = os.environ.get("REDDIT_USER_AGENT") or _DEFAULT_UA
    return cid, secret, ua


def reddit_configured() -> bool:
    return reddit_credentials() is not None


def reddit_user_agent() -> str:
    """Descriptive UA per Reddit's API rules (used by both the OAuth and RSS paths)."""
    return os.environ.get("REDDIT_USER_AGENT") or _DEFAULT_UA


# --- residential RSS fallback (default when no OAuth creds) -------------------

_ATOM = "{http://www.w3.org/2005/Atom}"
_SUB_RE = re.compile(r"/r/([A-Za-z0-9_]+)/")
# Rotate one group per poll (module-level so it advances across the pipeline
# loop's sweeps; resets on restart — harmless). Reddit's RSS rate limit is too
# harsh to poll all groups in one sweep, so we spread them over successive sweeps.
_rss_rotation = [0]


def parse_rss(xml_text: str) -> list[Any]:
    """A Reddit Atom /new feed -> NewsItems (pure; the testable core)."""
    from IngestionModule import NewsItem

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall(f"{_ATOM}entry"):
        title = (e.findtext(f"{_ATOM}title") or "").strip()
        link_el = e.find(f"{_ATOM}link")
        url = link_el.get("href") if link_el is not None else ""
        if not title or not url:
            continue
        entry_id = (e.findtext(f"{_ATOM}id") or "").strip()  # e.g. t3_abc -> dedup guid
        published = e.findtext(f"{_ATOM}published") or e.findtext(f"{_ATOM}updated")
        cat = e.find(f"{_ATOM}category")
        sub = (cat.get("term") if cat is not None else None) or (
            m.group(1) if (m := _SUB_RE.search(url)) else "reddit"
        )
        content = (e.findtext(f"{_ATOM}content") or "").strip()
        try:
            when = datetime.fromisoformat(str(published).replace("Z", "+00:00")).astimezone(UTC)
        except Exception:  # noqa: BLE001
            when = datetime.now(UTC)
        out.append(
            NewsItem(
                source=f"Reddit - {sub}",
                source_type="social",
                title=title,
                published_at=when,
                description=content or title,
                url=url,
                extra={"guid": entry_id or url, "subreddit": sub, "via": "rss"},
            )
        )
    return out


class RedditRSSExtractor:
    """Residential Atom-RSS poller. Polls ONE subreddit group per call (rotating)
    to respect Reddit's harsh per-IP RSS rate limit; a 429 is fail-soft (skip this
    cycle, the next sweep rotates on). ``http`` is injectable: ``get_text(url,
    headers) -> (status, text)`` (fake in tests, aiohttp-backed in production)."""

    def __init__(self, groups: list[list[str]] | None = None, *, limit: int = 25) -> None:
        self._groups = groups or REDDIT_GROUPS
        self._limit = limit
        self._ua = reddit_user_agent()

    def _next_group(self) -> list[str]:
        subs = self._groups[_rss_rotation[0] % len(self._groups)]
        _rss_rotation[0] += 1
        return subs

    async def poll(self, http: Any) -> list[Any]:
        subs = self._next_group()
        url = f"https://www.reddit.com/r/{'+'.join(subs)}/new/.rss?limit={self._limit}"
        try:
            status, text = await http.fetch(url, headers={"User-Agent": self._ua})
        except Exception as exc:  # noqa: BLE001 — one bad fetch must not crash the sweep
            log.warning("Reddit RSS fetch of %s failed (%s)", "+".join(subs), type(exc).__name__)
            return []
        if status != 200:
            log.warning(
                "Reddit RSS /r/%s HTTP %s (rate-limited?) — skip this cycle, rotate next sweep",
                "+".join(subs), status,
            )
            return []
        return parse_rss(text)


class RedditRSSHttp:
    """Production RSS client over one aiohttp session. Method is ``fetch`` (not
    ``get_text``) so it never collides with the backend _HttpClient's same-named
    but incompatible get_text (which returns a bare str) when the dispatch loop
    passes that client in — the discriminator that decides whether to reuse the
    injected http or open a fresh session."""

    def __init__(self, session: Any) -> None:
        self._s = session

    async def fetch(self, url, headers):
        async with self._s.get(url, headers=headers) as r:
            return r.status, (await r.text() if r.status == 200 else "")


def _news_item(post: dict[str, Any]) -> Any | None:
    """One Reddit listing child ({"kind","data"}) -> NewsItem, or None if unusable.
    NewsItem is imported lazily so this module stays importable without backend/."""
    from IngestionModule import NewsItem  # backend/ is on sys.path via dispatch

    d = post.get("data") or {}
    title = (d.get("title") or "").strip()
    created = d.get("created_utc")
    if not title or created is None:
        return None
    sub = d.get("subreddit") or "reddit"
    permalink = d.get("permalink") or ""
    url = f"https://www.reddit.com{permalink}" if permalink else (d.get("url") or "")
    # Fullname (t3_xxx) is Reddit's stable post id -> our dedup guid.
    guid = d.get("name") or (f"t3_{d.get('id')}" if d.get("id") else None)
    if not guid and not url:
        return None
    body = (d.get("selftext") or "").strip()
    return NewsItem(
        source=f"Reddit - {sub}",
        source_type="social",
        title=title,
        published_at=datetime.fromtimestamp(float(created), tz=UTC),
        description=body or title,
        url=url,
        extra={
            "guid": guid,
            "subreddit": sub,
            "reddit_score": d.get("score"),
            "num_comments": d.get("num_comments"),
            "permalink": permalink,
        },
    )


def parse_listing(data: dict[str, Any]) -> list[Any]:
    """A Reddit /new listing JSON -> NewsItems (pure; the testable core)."""
    children = ((data or {}).get("data") or {}).get("children") or []
    out = []
    for child in children:
        item = _news_item(child)
        if item is not None:
            out.append(item)
    return out


class RedditOAuthExtractor:
    """Read-only Reddit poller. ``http`` is an injectable async client with
    ``post_form(url, data, auth, headers) -> (status, json)`` and
    ``get_json(url, headers, params) -> (status, json)`` (a fake in tests; the
    aiohttp-backed :class:`RedditHttp` in production)."""

    def __init__(
        self,
        credentials: tuple[str, str, str],
        *,
        groups: list[list[str]] | None = None,
        limit: int = 100,
        clock: Any = None,
    ) -> None:
        self._cid, self._secret, self._ua = credentials
        self._groups = groups or REDDIT_GROUPS
        self._limit = limit
        self._clock = clock or (lambda: asyncio.get_event_loop().time())
        self._token: str | None = None
        self._token_exp: float = 0.0

    async def _ensure_token(self, http: Any) -> str | None:
        if self._token and self._clock() < self._token_exp:
            return self._token
        status, body = await http.post_form(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._cid, self._secret),
            headers={"User-Agent": self._ua},
        )
        if status != 200 or not body.get("access_token"):
            log.warning("Reddit token request failed (HTTP %s) — skipping this cycle", status)
            self._token = None
            return None
        self._token = body["access_token"]
        # Refresh a minute before expiry; default 1h if the field is missing.
        self._token_exp = self._clock() + float(body.get("expires_in", 3600)) - 60
        return self._token

    async def poll(self, http: Any) -> list[Any]:
        """Poll every subreddit group once; returns NewsItems. Per-group fail-soft."""
        token = await self._ensure_token(http)
        if token is None:
            return []
        headers = {"Authorization": f"bearer {token}", "User-Agent": self._ua}
        items: list[Any] = []
        for subs in self._groups:
            url = f"{API_BASE}/r/{'+'.join(subs)}/new"
            try:
                status, data = await http.get_json(
                    url, headers=headers, params={"limit": self._limit}
                )
                if status == 200:
                    items += parse_listing(data)
                else:
                    log.warning("Reddit /r/%s/new HTTP %s (continuing)", "+".join(subs), status)
            except Exception as exc:  # noqa: BLE001 — one group must not sink the rest
                log.warning("Reddit poll of %s failed (%s)", "+".join(subs), type(exc).__name__)
            await asyncio.sleep(1.0)  # polite pacing between group requests
        return items


class RedditHttp:
    """Production async client over one aiohttp session (matches the extractor's
    injectable interface)."""

    def __init__(self, session: Any) -> None:
        self._s = session

    async def post_form(self, url, data, auth, headers):
        import aiohttp

        ba = aiohttp.BasicAuth(auth[0], auth[1])
        async with self._s.post(url, data=data, auth=ba, headers=headers) as r:
            body = await r.json() if r.status < 500 else {}
            return r.status, body

    async def get_json(self, url, headers, params):
        async with self._s.get(url, headers=headers, params=params) as r:
            body = await r.json() if r.status == 200 else {}
            return r.status, body
