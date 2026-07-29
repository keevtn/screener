"""Google-Trends search-interest lane — a NEW descriptive attention axis.

ACCESS PATH (verified live from this box, 2026-07-20):
  * Official Google Trends API is a gated alpha (application-only, Google Cloud +
    OAuth, tight quotas) — not self-serve, a bottleneck like Reddit OAuth.
  * pytrends is ARCHIVED (Apr 2025) and unmaintained — not depended on.
  * The raw endpoints work from a RESIDENTIAL IP after a cookie-priming request
    (first hit 429s + sets NID, then explore->multiline returns 200). 14 sequential
    queries at 3s pacing held with zero 429s here.

So this is a MINIMAL custom client (cookie prime -> /api/explore for the
TIMESERIES widget token -> /api/widgetdata/multiline for the series), no
dependency, fail-soft, single-term per ticker. Single-term matters: Google
normalizes each term's series to that TERM's own trailing-window max, so the
value is the ticker's OWN 0-100 interest — the honest per-ticker baseline. Each
query returns the whole trailing series, so a ticker's history backfills on first
sight (the baseline clock starts ~90 days deep, not from zero).

Descriptive / SHADOW only — writes search_interest_daily, never a signal path.
The honest cross-time anomaly measure is search_interest_z (per-ticker z vs its
own history, like buzz_z), NOT the raw 0-100 (which isn't cross-ticker comparable).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, date as date_, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    RawItem,
    SearchInterestDaily,
)
from pipeline.common.timeutil import utcnow

log = logging.getLogger("pipeline.ingest.trends")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
EXPLORE_URL = "https://trends.google.com/trends/api/explore"
MULTILINE_URL = "https://trends.google.com/trends/api/widgetdata/multiline"


def search_trends_enabled() -> bool:
    """Kill-switch (no credential involved). Default ON — the baseline clock runs;
    set SEARCH_TRENDS_ENABLED=0 to disable if the endpoint turns hostile."""
    return (os.environ.get("SEARCH_TRENDS_ENABLED", "1") or "").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def _strip(text: str) -> dict[str, Any]:
    """Google prefixes JSON with )]}' — strip to the first brace and parse."""
    i = text.find("{")
    if i < 0:
        return {}
    try:
        return json.loads(text[i:])
    except Exception:  # noqa: BLE001
        return {}


def _timeline_raw(payload: dict[str, Any]) -> list[tuple[int, float]]:
    """multiline widget JSON -> [(epoch_seconds, value)] (shared extraction)."""
    tl = (payload.get("default") or {}).get("timelineData") or []
    out: list[tuple[int, float]] = []
    for p in tl:
        ts = p.get("time")
        vals = p.get("value") or []
        if ts is None or not vals:
            continue
        try:
            out.append((int(ts), float(vals[0])))
        except Exception:  # noqa: BLE001
            continue
    return out


def parse_timeline(payload: dict[str, Any]) -> list[tuple[date_, float]]:
    """Daily series -> [(date, own-normalized 0-100)] (pure/testable)."""
    return [(datetime.fromtimestamp(ts, tz=UTC).date(), v) for ts, v in _timeline_raw(payload)]


def parse_timeline_hourly(payload: dict[str, Any]) -> list[tuple[datetime, float]]:
    """Hourly series -> [(UTC datetime, own-normalized 0-100)] (pure/testable)."""
    return [(datetime.fromtimestamp(ts, tz=UTC), v) for ts, v in _timeline_raw(payload)]


class _RequestsHttp:
    """Production client over one requests.Session that primes Google's cookie."""

    def __init__(self) -> None:
        import requests

        self._s = requests.Session()
        self._s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US"})
        self._primed = False

    def get(self, url: str, params: dict[str, Any]) -> tuple[int, str]:
        if not self._primed:
            try:
                self._s.get("https://trends.google.com/?geo=US", timeout=15)
            except Exception:  # noqa: BLE001 — priming best-effort
                pass
            self._primed = True
        r = self._s.get(url, params=params, timeout=20)
        return r.status_code, (r.text if r.status_code == 200 else "")


class GoogleTrendsClient:
    """Minimal Trends client. ``http`` is injectable: get(url, params) ->
    (status, text) (fake in tests, cookie-priming requests in production)."""

    def __init__(self, http: Any = None, *, geo: str = "US", timeframe: str = "today 3-m",
                 tz: int = -240) -> None:
        self._http = http or _RequestsHttp()
        self._geo = geo
        self._tf = timeframe
        self._tz = tz

    def _fetch(self, term: str, timeframe: str, parser) -> tuple[Any, list | None]:
        """explore (TIMESERIES token) -> multiline series, parsed by `parser`.
        Returns (200, series) or (status/tag, None)."""
        req = {
            "comparisonItem": [{"keyword": term, "geo": self._geo, "time": timeframe}],
            "category": 0,
            "property": "",
        }
        status, text = self._http.get(
            EXPLORE_URL, {"hl": "en-US", "tz": self._tz, "req": json.dumps(req)}
        )
        if status != 200:
            return status, None
        widget = next(
            (w for w in _strip(text).get("widgets", []) if w.get("id") == "TIMESERIES"), None
        )
        if not widget or "token" not in widget or "request" not in widget:
            return "no-timeseries", None
        status2, text2 = self._http.get(
            MULTILINE_URL,
            {"hl": "en-US", "tz": self._tz, "req": json.dumps(widget["request"]),
             "token": widget["token"]},
        )
        if status2 != 200:
            return status2, None
        return 200, parser(_strip(text2))

    def interest_series(self, term: str) -> tuple[Any, list[tuple[date_, float]] | None]:
        """Daily own-normalized series (status, [(date, value)] | None)."""
        return self._fetch(term, self._tf, parse_timeline)

    def interest_hourly(
        self, term: str, *, timeframe: str = "now 7-d"
    ) -> tuple[Any, list[tuple[datetime, float]] | None]:
        """HOURLY own-normalized series (status, [(UTC datetime, value)] | None).
        'now 7-d' returns hourly resolution; the panel slices the tail it wants."""
        return self._fetch(term, timeframe, parse_timeline_hourly)


def hot_set(
    session: Session, *, limit: int = 40, days: int = 3, watchlist: list[str] | None = None
) -> list[str]:
    """The bounded hot set to snapshot: an env/arg watchlist first, then tickers
    with the most material recent catalysts — capped at ``limit`` (never the
    universe). Deterministic order so daily runs are stable."""
    picks: list[str] = []
    seen: set[str] = set()
    wl = watchlist if watchlist is not None else _env_watchlist()
    for t in wl:
        u = t.strip().upper()
        if u and u not in seen:
            seen.add(u)
            picks.append(u)
    cutoff = utcnow().date().toordinal() - days
    rows = session.execute(
        select(ClusterEntity.ticker, ClusterScore.materiality)
        .join(Cluster, Cluster.cluster_id == ClusterEntity.cluster_id)
        .join(ClusterScore, ClusterScore.cluster_id == Cluster.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .where(RawItem.published_at >= datetime.fromordinal(cutoff).replace(tzinfo=UTC))
    ).all()
    best: dict[str, float] = {}
    for ticker, mat in rows:
        best[ticker] = max(best.get(ticker, 0.0), mat or 0.0)
    for ticker, _ in sorted(best.items(), key=lambda kv: kv[1], reverse=True):
        if len(picks) >= limit:
            break
        if ticker not in seen:
            seen.add(ticker)
            picks.append(ticker)
    return picks[:limit]


def _env_watchlist() -> list[str]:
    raw = os.environ.get("SEARCH_TRENDS_WATCHLIST", "")
    return [t for t in (x.strip() for x in raw.split(",")) if t]


def snapshot_search_interest(
    session: Session,
    tickers: list[str],
    client: GoogleTrendsClient,
    *,
    term_fmt: str = "{} stock",
    now: datetime | None = None,
    pace_s: float = 3.0,
    sleep=time.sleep,
    max_consecutive_429: int = 3,
) -> dict[str, int]:
    """Snapshot each ticker's own-normalized search series into search_interest_daily
    (upsert by ticker+date — the full trailing series backfills history). Fail-soft
    and paced; a run of consecutive 429s stops the run early (partial coverage is
    fine — the baseline accrues across days)."""
    now = now or utcnow()
    stats = {"tickers": 0, "rows": 0, "failed": 0, "rate_limited": 0}
    consec = 0
    for tk in tickers:
        term = term_fmt.format(tk)
        try:
            status, series = client.interest_series(term)
        except Exception as exc:  # noqa: BLE001 — one bad fetch never sinks the run
            log.warning("search-trends fetch failed for %s (%s)", tk, type(exc).__name__)
            stats["failed"] += 1
            sleep(pace_s)
            continue
        if status == 429:
            consec += 1
            stats["rate_limited"] += 1
            log.warning("search-trends 429 for %s (%d consecutive)", tk, consec)
            if consec >= max_consecutive_429:
                log.warning("search-trends: %d consecutive 429s — stopping early (partial run)", consec)
                break
            sleep(pace_s * 2)
            continue
        consec = 0
        if status != 200 or not series:
            log.info("search-trends: no series for %s (status=%s)", tk, status)
            stats["failed"] += 1
            sleep(pace_s)
            continue
        for d, v in series:
            session.execute(
                sqlite_insert(SearchInterestDaily)
                .values(ticker=tk, date=d, interest=v, term=term, source="google_trends",
                        updated_at=now)
                .on_conflict_do_update(
                    index_elements=[SearchInterestDaily.ticker, SearchInterestDaily.date],
                    set_={"interest": v, "term": term, "updated_at": now},
                )
            )
            stats["rows"] += 1
        session.commit()
        stats["tickers"] += 1
        sleep(pace_s)
    return stats


_HOURLY_TTL = 1800.0  # 30 min — Trends hourly moves slowly AND we must not hammer.
_HOURLY_ERR_TTL = 300.0
_hourly_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def hourly_interest(
    ticker: str,
    *,
    client: GoogleTrendsClient,
    hours: int = 48,
    cache: dict | None = None,
    ttl: float = _HOURLY_TTL,
    now: float | None = None,
    term_fmt: str = "{} stock",
) -> dict[str, Any]:
    """On-demand HOURLY own-normalized search interest for ONE ticker (mirror of
    the 1m-candles endpoint): TTL-cached, fail-soft, never bulk — the unofficial
    endpoint won't tolerate polling the whole hot set hourly.

    Returns {ticker, source: 'google_trends'|'unavailable', label, points}. Each
    point is {hour (UTC ISO), value} where value is Google's own-term relative
    0-100 — NOT a count. Empty points + source 'unavailable' on 429/error, so the
    panel shows an honest label instead of faking data."""
    cache = _hourly_cache if cache is None else cache
    now = now if now is not None else time.time()
    tk = ticker.upper()
    key = f"{tk}:{hours}"
    hit = cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    label = "relative interest (0-100, own-term)"
    try:
        status, series = client.interest_hourly(term_fmt.format(tk))
    except Exception as exc:  # noqa: BLE001 — panel must never 500 on a vendor hiccup
        log.warning("hourly search interest failed for %s (%s)", tk, type(exc).__name__)
        status, series = "error", None
    if status != 200 or not series:
        result = {"ticker": tk, "source": "unavailable", "label": label,
                  "note": f"trends {status}", "points": []}
        cache[key] = (now + _HOURLY_ERR_TTL, result)  # short cache on failure -> retry sooner
        return result
    pts = [{"hour": dt.isoformat(), "value": v} for dt, v in series[-hours:]]
    result = {"ticker": tk, "source": "google_trends", "label": label, "points": pts}
    cache[key] = (now + ttl, result)
    return result


def own_z(hist: list[float], today_val: float | None, *, min_days: int = 10) -> float | None:
    """Per-series anomaly z: (today - mean(hist)) / std(hist), rounded. None until
    >= min_days of history and non-degenerate variance. Pure — shared by the
    single-ticker DB path and the batched screener path so both agree exactly."""
    if today_val is None or len(hist) < min_days:
        return None
    mean = sum(hist) / len(hist)
    var = sum((x - mean) ** 2 for x in hist) / len(hist)
    std = var ** 0.5
    if std < 1e-6:
        return None
    return round((today_val - mean) / std, 3)


def search_interest_z(
    session: Session, ticker: str, *, today: date_ | None = None, min_days: int = 10
) -> float | None:
    """Per-ticker anomaly z vs its OWN history — the honest normalization (the raw
    0-100 isn't cross-ticker comparable). None until >= min_days of history and
    non-degenerate variance. Excludes today's partial-day point from the baseline."""
    today = today or utcnow().date()
    rows = session.execute(
        select(SearchInterestDaily.date, SearchInterestDaily.interest)
        .where(SearchInterestDaily.ticker == ticker.upper())
        .order_by(SearchInterestDaily.date)
    ).all()
    hist = [r.interest for r in rows if r.date < today]
    today_val = next((r.interest for r in rows if r.date == today), None)
    return own_z(hist, today_val, min_days=min_days)
