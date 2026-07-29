"""Screener rows + per-ticker vs-own-history stats (the NEWS screener's data plane).

Two server-side aggregations over the SQLite spine, both windowed + indexed so they
stay fast as history grows:

- ``ticker_stats``: per-ticker vs-own-history stats (attention_daily rollup + buzz
  baselines + search interest). Extracted verbatim from the /screener/stats endpoint
  so both it and the rows endpoint share one implementation. Ratios/z-scores are null
  until a ticker has enough own-history — honest "—", never a fabricated baseline.

- ``screener_rows``: one row per UNIVERSE ticker (latest fundamentals snapshot) with
  >= 1 attributed cluster in the last ``hours`` window. This is the coverage set the
  Mongo /api/news window can't see: ~hundreds of universe names with recent attributed
  news, vs the ~dozens visible in a 1000-item news window. Aggregates mentions, distinct
  sources, both-axis sentiment (finbert + lm kept SEPARATE, I7), and folds in buzz_z,
  fundamentals, and the full vs-own-history stats. The catalyst context looks back
  further than the mentions window (``catalyst_lookback_days``) so a still-relevant
  catalyst (e.g. an M&A a few days old) surfaces even when the last 48h is quiet.

Both drive from windowed, indexed scans (raw_items.published_at / cluster_scores
catalyst index) rather than a full cluster_entities scan, and batch every follow-up
lookup (NO per-ticker N+1).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.aggregate.attention import buzz_z
from pipeline.common.models import (
    AttentionDaily,
    BuzzBaseline,
    Cluster,
    ClusterEntity,
    ClusterScore,
    Entity,
    FundamentalsSnapshot,
    RawItem,
    SearchInterestDaily,
)
from pipeline.common.timeutil import utcnow

# SQLite caps host parameters per statement (default 999); chunk IN() lists well
# under it. The row set can grow past 999 tickers as coverage widens, so every
# batched lookup here chunks rather than binding the whole set at once.
_CHUNK = 900


def _chunks(items: list[str], n: int = _CHUNK) -> list[list[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def ticker_stats(
    session: Session, tickers: set[str] | list[str], *, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Per-ticker vs-own-history stats (attention_daily + buzz baselines + search).

    History excludes today (a partial day); ratios/z-scores are null until a ticker
    has >= 5 observed days (search_z needs >= 10) — honest null, never a fabricated
    baseline. Batched: a handful of chunked IN() queries, no per-ticker N+1.
    """
    from pipeline.ingest.trends import own_z

    wanted = {t.strip().upper() for t in tickers if t and t.strip()}
    if not wanted:
        return {}
    today = (now or utcnow()).date()
    wanted_list = sorted(wanted)

    hist: dict[str, list[tuple[int, float | None]]] = {}
    today_row: dict[str, tuple[int, int, float | None]] = {}
    baselines: dict[str, BuzzBaseline] = {}
    si_hist: dict[str, list[float]] = {}
    si_today: dict[str, float] = {}
    for chunk in _chunks(wanted_list):
        for r in session.execute(
            select(AttentionDaily).where(AttentionDaily.ticker.in_(chunk))
        ).scalars():
            total = (r.struct_count or 0) + (r.social_count or 0)
            if r.date == today:
                today_row[r.ticker] = (r.struct_count or 0, r.social_count or 0, r.sentiment_mean)
            else:
                hist.setdefault(r.ticker, []).append((total, r.sentiment_mean))
        for b in session.execute(
            select(BuzzBaseline).where(BuzzBaseline.ticker.in_(chunk))
        ).scalars():
            baselines[b.ticker] = b
        # today's partial-day search point is excluded from the baseline, like the
        # mention/sentiment z above. Ordered by date so si_hist accrues chronologically.
        for tk_si, d_si, v_si in session.execute(
            select(
                SearchInterestDaily.ticker,
                SearchInterestDaily.date,
                SearchInterestDaily.interest,
            )
            .where(SearchInterestDaily.ticker.in_(chunk))
            .order_by(SearchInterestDaily.date)
        ).all():
            if d_si == today:
                si_today[tk_si] = v_si
            else:
                si_hist.setdefault(tk_si, []).append(v_si)

    out: dict[str, dict[str, Any]] = {}
    for t in wanted:
        h = hist.get(t, [])
        n_days = len(h)
        struct_t, social_t, sent_t = today_row.get(t, (0, 0, None))
        mentions_today = struct_t + social_t
        avg = mentions_x = None
        sent_mean = sent_std = sent_z = None
        if n_days >= 5:
            counts = [c for c, _ in h]
            avg = sum(counts) / n_days
            if avg > 0:
                mentions_x = round(mentions_today / avg, 2)
            sents = [s for _, s in h if s is not None]
            if len(sents) >= 5:
                sent_mean = sum(sents) / len(sents)
                svar = sum((s - sent_mean) ** 2 for s in sents) / len(sents)
                sent_std = svar**0.5
                if sent_t is not None and sent_std > 0.01:
                    sent_z = round((sent_t - sent_mean) / sent_std, 2)
        b = baselines.get(t)
        out[t] = {
            "n_days": n_days,
            "avg_daily_mentions": round(avg, 2) if avg is not None else None,
            "mentions_today": mentions_today,
            "struct_today": struct_t,
            "social_today": social_t,
            "mentions_x_normal": mentions_x,
            "sent_today": round(sent_t, 3) if sent_t is not None else None,
            "sent_hist_mean": round(sent_mean, 3) if sent_mean is not None else None,
            "sent_hist_std": round(sent_std, 3) if sent_std is not None else None,
            "sent_z": sent_z,
            "buzz_baseline": (
                {"mean": b.mean, "std": b.std, "n_days": b.n_days, "source": b.source}
                if b
                else None
            ),
            "search_z": own_z(si_hist.get(t, []), si_today.get(t)),
            "search_today": (round(si_today[t], 1) if t in si_today else None),
            "search_days": len(si_hist.get(t, [])),
        }
    return out


def _universe_tickers(session: Session) -> set[str]:
    """Tickers in the latest fundamentals snapshot — the tradeable universe the
    screener scopes to (drops off-universe cashtag noise: indices, crypto, bad matches)."""
    latest = select(FundamentalsSnapshot.as_of).order_by(FundamentalsSnapshot.as_of.desc()).limit(1)
    latest_as_of = session.execute(latest).scalar_one_or_none()
    if latest_as_of is None:
        return set()
    return {
        t
        for (t,) in session.execute(
            select(FundamentalsSnapshot.ticker).where(FundamentalsSnapshot.as_of == latest_as_of)
        ).all()
    }


def _last_catalyst_by_ticker(
    session: Session, *, cutoff: datetime, universe: set[str], ref: datetime
) -> dict[str, dict[str, Any]]:
    """Most-recent catalyst per UNIVERSE ticker over a broader lookback (``cutoff``).

    Drives from the sparse cluster_scores.catalyst_type index (not a full scan), then
    batches the cluster->ticker map. The lookback is intentionally wider than the
    mentions window so a still-relevant catalyst surfaces even in a window-quiet name.
    """
    rows = session.execute(
        select(
            Cluster.cluster_id,
            RawItem.published_at,
            ClusterScore.catalyst_type,
            ClusterScore.event_stage,
            ClusterScore.high_alert,
            ClusterScore.created_at,
        )
        .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .where(ClusterScore.catalyst_type.is_not(None))
        .where(RawItem.published_at >= cutoff)
    ).all()
    if not rows:
        return {}
    by_cluster = {r.cluster_id: r for r in rows}
    cluster_ids = list(by_cluster)
    best: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(cluster_ids):
        for cid, ticker in session.execute(
            select(ClusterEntity.cluster_id, ClusterEntity.ticker).where(
                ClusterEntity.cluster_id.in_(chunk)
            )
        ).all():
            if ticker not in universe:
                continue
            r = by_cluster[cid]
            prev = best.get(ticker)
            if prev is None or r.published_at > prev["_pub"]:
                best[ticker] = {
                    "_pub": r.published_at,
                    "catalyst_type": r.catalyst_type,
                    "event_stage": r.event_stage,
                    "high_alert": bool(r.high_alert),
                    "published_at": r.published_at.isoformat(),
                    "called_at": r.created_at.isoformat() if r.created_at else None,
                    "age_hours": round((ref - r.published_at).total_seconds() / 3600.0, 1),
                }
    for v in best.values():
        v.pop("_pub", None)
    return best


def _buzz_by_ticker(session: Session, tickers: list[str]) -> dict[str, float]:
    """Latest-day buzz-z per ticker (most recent attention_daily social vs its own
    baseline). Only tickers with a baseline get a value. Batched, no N+1."""
    if not tickers:
        return {}
    out: dict[str, float] = {}
    for chunk in _chunks(tickers):
        baselines = {
            b.ticker: b
            for b in session.execute(
                select(BuzzBaseline).where(BuzzBaseline.ticker.in_(chunk))
            ).scalars()
        }
        if not baselines:
            continue
        latest_social: dict[str, tuple[Any, int]] = {}
        for tk, d, social in session.execute(
            select(AttentionDaily.ticker, AttentionDaily.date, AttentionDaily.social_count).where(
                AttentionDaily.ticker.in_(list(baselines))
            )
        ).all():
            prev = latest_social.get(tk)
            if prev is None or d > prev[0]:
                latest_social[tk] = (d, social or 0)
        for tk, (_d, social) in latest_social.items():
            z = buzz_z(social, baselines.get(tk))
            if z is not None:
                out[tk] = z
    return out


def _fundamentals_by_ticker(session: Session, tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Latest fundamentals + Entity name for the row set (sector/industry/mcap/vol/
    short/beta). Every row ticker is in the latest snapshot by construction, so this
    is always populated (individual fields may still be null)."""
    if not tickers:
        return {}
    latest_as_of = session.execute(
        select(FundamentalsSnapshot.as_of).order_by(FundamentalsSnapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if latest_as_of is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(tickers):
        for f, name in session.execute(
            select(FundamentalsSnapshot, Entity.canonical_name)
            .outerjoin(Entity, Entity.ticker == FundamentalsSnapshot.ticker)
            .where(
                FundamentalsSnapshot.as_of == latest_as_of,
                FundamentalsSnapshot.ticker.in_(chunk),
            )
        ).all():
            out[f.ticker] = {
                "name": name,
                "sector": f.sector,
                "industry": f.industry,
                "market_cap": f.market_cap,
                "avg_volume": f.avg_volume,
                "short_float": f.short_float,
                "beta": f.beta,
            }
    return out


def screener_rows(
    session: Session,
    *,
    hours: int = 48,
    now: datetime | None = None,
    catalyst_lookback_days: int = 30,
) -> dict[str, Any]:
    """One row per universe ticker with >= 1 attributed cluster in the last ``hours``.

    Window-driven: the in-window cluster set is read via raw_items.published_at (an
    indexed, window-bounded scan), then cluster->ticker attribution is batched. Per
    ticker we aggregate mentions (distinct in-window clusters), distinct sources, and
    both-axis sentiment (finbert + lm — mean AND latest, kept SEPARATE, never
    pre-blended per I7). buzz_z, fundamentals, the last catalyst (wider lookback), and
    the full vs-own-history stats are folded in via batched lookups.
    """
    ref = now or utcnow()
    cutoff = ref - timedelta(hours=hours)
    universe = _universe_tickers(session)
    empty = {"window_hours": hours, "count": 0, "rows": []}
    if not universe:
        return empty

    # In-window clusters (origin published_at in window) — the raw_items index drives
    # this, so cost scales with the window, not total history. LEFT join scores: an
    # unscored cluster still counts as coverage (mention), just with null sentiment.
    win_rows = session.execute(
        select(
            Cluster.cluster_id,
            RawItem.source,
            RawItem.published_at,
            ClusterScore.finbert_score,
            ClusterScore.lm_score,
            ClusterScore.catalyst_type,
            ClusterScore.high_alert,
        )
        .join(Cluster, Cluster.origin_item_id == RawItem.id)
        .outerjoin(ClusterScore, ClusterScore.cluster_id == Cluster.cluster_id)
        .where(RawItem.published_at >= cutoff)
    ).all()
    if not win_rows:
        return empty
    by_cluster = {r.cluster_id: r for r in win_rows}

    # cluster -> attributed tickers, batched over the in-window cluster ids.
    cluster_tickers: dict[str, list[str]] = {}
    cluster_ids = list(by_cluster)
    for chunk in _chunks(cluster_ids):
        for cid, ticker in session.execute(
            select(ClusterEntity.cluster_id, ClusterEntity.ticker).where(
                ClusterEntity.cluster_id.in_(chunk)
            )
        ).all():
            if ticker in universe:
                cluster_tickers.setdefault(cid, []).append(ticker)

    # Fold per ticker in a single pass over the in-window (cluster, ticker) pairs.
    agg: dict[str, dict[str, Any]] = {}
    for cid, tickers in cluster_tickers.items():
        r = by_cluster[cid]
        for t in tickers:
            a = agg.get(t)
            if a is None:
                a = agg[t] = {
                    "clusters": set(),
                    "sources": set(),
                    "fin_sum": 0.0,
                    "fin_n": 0,
                    "lm_sum": 0.0,
                    "lm_n": 0,
                    "fin_latest": None,
                    "fin_latest_at": None,
                    "lm_latest": None,
                    "lm_latest_at": None,
                    "high_alert": False,
                    "catalyst_in_window": False,
                    "latest_at": None,
                }
            a["clusters"].add(cid)
            if r.source:
                a["sources"].add(r.source)
            if r.finbert_score is not None:
                a["fin_sum"] += r.finbert_score
                a["fin_n"] += 1
                if a["fin_latest_at"] is None or r.published_at > a["fin_latest_at"]:
                    a["fin_latest"] = r.finbert_score
                    a["fin_latest_at"] = r.published_at
            if r.lm_score is not None:
                a["lm_sum"] += r.lm_score
                a["lm_n"] += 1
                if a["lm_latest_at"] is None or r.published_at > a["lm_latest_at"]:
                    a["lm_latest"] = r.lm_score
                    a["lm_latest_at"] = r.published_at
            if r.high_alert:
                a["high_alert"] = True
            if r.catalyst_type is not None:
                a["catalyst_in_window"] = True
            if a["latest_at"] is None or r.published_at > a["latest_at"]:
                a["latest_at"] = r.published_at

    if not agg:
        return empty

    row_tickers = sorted(agg)
    stats = ticker_stats(session, row_tickers, now=ref)
    buzz = _buzz_by_ticker(session, row_tickers)
    funds = _fundamentals_by_ticker(session, row_tickers)
    last_cat = _last_catalyst_by_ticker(
        session,
        cutoff=ref - timedelta(days=catalyst_lookback_days),
        universe=universe,
        ref=ref,
    )

    rows = []
    for t in row_tickers:
        a = agg[t]
        rows.append(
            {
                "ticker": t,
                "mentions": len(a["clusters"]),
                "sources": len(a["sources"]),
                "finbert_mean": round(a["fin_sum"] / a["fin_n"], 4) if a["fin_n"] else None,
                "finbert_latest": (
                    round(a["fin_latest"], 4) if a["fin_latest"] is not None else None
                ),
                "lm_mean": round(a["lm_sum"] / a["lm_n"], 4) if a["lm_n"] else None,
                "lm_latest": round(a["lm_latest"], 4) if a["lm_latest"] is not None else None,
                "latest_at": a["latest_at"].isoformat() if a["latest_at"] else None,
                "catalyst_in_window": a["catalyst_in_window"],
                "high_alert": a["high_alert"],
                "buzz_z": buzz.get(t),
                "last_catalyst": last_cat.get(t),
                "fundamentals": funds.get(t),
                "stats": stats.get(t),
            }
        )
    # Busiest first — a stable default; the frontend re-sorts by the chosen RANK BY.
    rows.sort(key=lambda r: r["mentions"], reverse=True)
    return {"window_hours": hours, "count": len(rows), "rows": rows}
