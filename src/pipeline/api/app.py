"""Read-only FastAPI app (docs/ROADMAP.md task 5.4).

Endpoints: /predictions (filter+paginate), /metrics, /tickers/{t}/state,
/clusters/{id}, /health. Pydantic response models are shared with the future
frontend. Read-only: no endpoint writes. Injectable engine for tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pipeline.agents import (
    ALLOWED_MODELS,
    DEFAULT_DEEP_DIVE_MODEL,
    DEFAULT_RANKER_MODEL,
    DeepDiveRateLimited,
    LLMClient,
    SoftCapExceeded,
    build_candidate_filter,
    deep_dive_rate_status,
    default_daily_cap,
    latest_analysis,
    resolve_model,
    run_deep_dive,
    run_ranking,
    spend_since,
)
from pipeline.aggregate.attention import buzz_z
from pipeline.aggregate.screener import screener_rows, ticker_stats
from pipeline.common.approval import ApprovalError, approve_change, reject_change
from pipeline.common.config import get_or_create_config
from pipeline.common.db import ensure_indexes, make_engine
from pipeline.common.events import event_stream, intraday_counts, publish_event
from pipeline.common.models import (
    AttentionDaily,
    BuzzBaseline,
    Cluster,
    ClusterEntity,
    ClusterScore,
    Config,
    Entity,
    FundamentalsSnapshot,
    LlmSpend,
    PendingChange,
    Prediction,
    PredictionContext,
    PremarketPanel,
    Ranking,
    RankingRun,
    RawItem,
    ScheduledEvent,
    SignalObservation,
    SimConfig,
    SimDailySummary,
    SimTrade,
    TickerAnalysis,
)
from pipeline.common.timeutil import utcnow
from pipeline.grade.metrics import metrics_by_config
from pipeline.lab.analysis import car_curves, load_lab_rows, quintile_spread, spearman_ic
from pipeline.marketdata import MarketDataProvider
from pipeline.marketdata.alpaca import AlpacaData, alpaca_configured
from pipeline.marketdata.paper_account import paper_reader
from pipeline.panel import (
    compile_preset,
    fired_panel,
    load_presets,
    premarket_panel,
    scheduled_panel,
    screen,
)
from pipeline.panel.premarket import ET as PMR_ET
from pipeline.panel.premarket import OPEN_ET as PMR_OPEN_ET

log = logging.getLogger("pipeline.api")


# --- response models ---------------------------------------------------------
class PredictionOut(BaseModel):
    prediction_id: str
    ticker: str
    direction: str
    confidence: float
    horizon_trading_days: int
    threshold: float
    issued_at: datetime
    config_version: str
    status: str
    outcome: str | None = None
    realized_adjusted_return: float | None = None
    graded_at: datetime | None = None  # when the grader resolved it (for "newly graded" badges)
    evidence: dict[str, Any] = {}
    # Origin-news context from the companion table (LEDGER lanes). null when the
    # originating cluster couldn't be resolved — honest, never guessed.
    source_class: str | None = None  # 'structured' | 'social' | 'mixed' — the lane
    headline: str | None = None  # originating article title
    url: str | None = None  # originating article link (rendered as a safe external link)
    source: str | None = None  # originating source name
    # A baseline is a SHADOW of a real prediction (always_up / random / momentum),
    # sharing the real's ticker+issued_at+origin headline but with its own direction —
    # the measurement machinery, not a signal. The LEDGER hides these by default.
    is_baseline: bool = False
    baseline_kind: str | None = None  # 'always_up' | 'random' | 'momentum' when is_baseline


class PredictionPage(BaseModel):
    count: int
    items: list[PredictionOut]


class MetricsOut(BaseModel):
    config_version: str
    total_graded: int
    correct: int
    incorrect: int
    expired: int
    hit_rate: float | None
    coverage: float | None
    precision: dict[str, float | None]
    recall: dict[str, float | None]
    mean_lead_time_days: float | None


class ClusterEntityOut(BaseModel):
    ticker: str
    ticker_role: str
    match_method: str


class ClusterOut(BaseModel):
    cluster_id: str
    origin_item_id: str
    origin_source: str | None
    origin_title: str | None
    origin_tier: int | None
    member_count: int
    finbert_score: float | None = None
    lm_score: float | None = None
    materiality: float | None = None
    catalyst_type: str | None = None
    event_stage: str | None = None
    entities: list[ClusterEntityOut] = []


class TickerStateOut(BaseModel):
    ticker: str
    attributed_clusters: int
    open_predictions: list[PredictionOut]
    recent_clusters: list[str]


class HealthOut(BaseModel):
    now: datetime
    raw_items: int
    clusters: int
    predictions: int
    last_ingested_at: datetime | None
    staleness_seconds: float | None
    per_source_class: dict[str, datetime | None]
    error_counts: dict[str, int] = {}
    # Bluesky firehose liveness (from its heartbeat file) — a dead stream is loud,
    # not silent, since social continuity is the Phase-6 baseline clock.
    firehose: dict[str, Any] | None = None


class RankingItemOut(BaseModel):
    rank: int
    ticker: str
    direction: str
    conviction: float
    rationale: str
    evidence_ids: list[str] = []


class RankingRunOut(BaseModel):
    run_id: str
    created_at: datetime
    trigger: str
    model: str
    horizon_trading_days: int
    candidate_count: int
    status: str
    config_version: str | None = None
    error: str | None = None
    items: list[RankingItemOut] = []


class RankRunRequest(BaseModel):
    """Force-run body: operator picks the model + timeframe and candidate terms."""

    model: str = DEFAULT_RANKER_MODEL
    horizon_trading_days: int | None = Field(default=None, ge=1, le=20)
    presets: list[str] = []
    high_alert: bool = True
    extreme_sentiment: bool = True
    # Candidate breadth. None -> AGENT_RANKER_CANDIDATES default (50); cap at 150
    # keeps a single run's input tokens (~450/candidate) well under the soft cap.
    limit: int | None = Field(default=None, ge=1, le=150)


class SpendOut(BaseModel):
    today_usd: float
    total_usd: float
    calls: int
    cap_usd: float  # daily soft cap (AGENT_DAILY_USD_CAP, default $2)
    pct_of_cap: float  # today_usd / cap_usd, clamped to [0, ∞)


# --- config panel (task 7.3): immutable versions + human-gated approvals -------
class ConfigOut(BaseModel):
    """A single immutable config version with its full params blob."""

    config_version: str
    created_at: datetime
    notes: str | None = None
    is_current: bool
    params: dict[str, Any]


class ConfigVersionOut(BaseModel):
    """History-row view of a config version (no params blob — fetch per-version)."""

    config_version: str
    created_at: datetime
    notes: str | None = None
    is_current: bool
    from_proposal: bool  # a pending_change was approved into this version


class ApproveRequest(BaseModel):
    notes: str = ""


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1)


class WatchlistPinRequest(BaseModel):
    """Pin a ticker to the TRADER watchlist (our DB, view/stage only)."""

    ticker: str = Field(..., min_length=1, max_length=12)
    note: str | None = None


class DeepDiveEvidenceOut(BaseModel):
    point: str
    cluster_id: str | None = None


class TickerAnalysisOut(BaseModel):
    """A persisted single-ticker deep-dive analysis (own-data AI read)."""

    analysis_id: str
    ticker: str
    created_at: datetime
    model: str
    horizon_trading_days: int
    config_version: str | None = None
    status: str
    direction: str | None = None
    conviction: float | None = None
    thesis: str | None = None
    key_evidence: list[DeepDiveEvidenceOut] = []
    risks: list[str] = []
    what_would_change_my_mind: list[str] = []
    evidence: dict[str, Any] = {}
    error: str | None = None


class DeepDiveRunRequest(BaseModel):
    """Force a deep dive: operator picks the model + timeframe (own-data only)."""

    model: str = DEFAULT_DEEP_DIVE_MODEL
    horizon_trading_days: int | None = Field(default=None, ge=1, le=20)


def _analysis_out(a: TickerAnalysis) -> TickerAnalysisOut:
    return TickerAnalysisOut(
        analysis_id=a.analysis_id,
        ticker=a.ticker,
        created_at=a.created_at,
        model=a.model,
        horizon_trading_days=a.horizon_trading_days,
        config_version=a.config_version,
        status=a.status,
        direction=a.direction,
        conviction=a.conviction,
        thesis=a.thesis,
        key_evidence=[DeepDiveEvidenceOut(**e) for e in (a.key_evidence_json or [])],
        risks=a.risks_json or [],
        what_would_change_my_mind=a.what_would_change_json or [],
        evidence=a.evidence_json or {},
        error=a.error,
    )


def _ranking_run_out(run: RankingRun, items: list[Ranking]) -> RankingRunOut:
    return RankingRunOut(
        run_id=run.run_id,
        created_at=run.created_at,
        trigger=run.trigger,
        model=run.model,
        horizon_trading_days=run.horizon_trading_days,
        candidate_count=run.candidate_count,
        status=run.status,
        config_version=run.config_version,
        error=run.error,
        items=[
            RankingItemOut(
                rank=i.rank,
                ticker=i.ticker,
                direction=i.direction,
                conviction=i.conviction,
                rationale=i.rationale,
                evidence_ids=i.evidence_ids_json or [],
            )
            for i in items
        ],
    )


def _prediction_out(
    p: Prediction, ctx: PredictionContext | None = None, baseline_kind: str | None = None
) -> PredictionOut:
    return PredictionOut(
        prediction_id=p.prediction_id,
        ticker=p.ticker,
        direction=p.direction,
        confidence=p.confidence,
        horizon_trading_days=p.horizon_trading_days,
        threshold=p.threshold,
        issued_at=p.issued_at,
        config_version=p.config_version,
        status=p.status,
        outcome=p.outcome,
        realized_adjusted_return=p.realized_adjusted_return,
        graded_at=p.graded_at,
        evidence=p.evidence_json or {},
        source_class=ctx.source_class if ctx else None,
        headline=ctx.headline if ctx else None,
        url=ctx.url if ctx else None,
        source=ctx.source if ctx else None,
        is_baseline=baseline_kind is not None,
        baseline_kind=baseline_kind,
    )


def _baseline_configs(session: Session) -> dict[str, str]:
    """config_version -> baseline kind ('always_up'|'random'|'momentum') for the
    baseline (shadow) configs. A config is a baseline iff its params carry a
    'baseline' key (see grade.baselines.ensure_baseline_configs). Real configs are
    absent. The configs table is tiny, so this is a cheap per-request lookup."""
    out: dict[str, str] = {}
    for cv, params in session.execute(select(Config.config_version, Config.params_json)).all():
        if isinstance(params, dict) and params.get("baseline"):
            out[cv] = str(params["baseline"])
    return out


def create_app(engine: Engine | None = None, *, llm_client: LLMClient | None = None) -> FastAPI:
    engine = engine or make_engine()
    ensure_indexes(engine)  # backfill perf indexes onto a long-lived DB (idempotent)
    from pipeline.api.watchlist import ensure_watchlist_table

    ensure_watchlist_table(engine)  # self-heal the Phase-3 watchlist table on boot
    app = FastAPI(title="Market News Prediction API", version="1", docs_url="/docs")

    # The Next.js dashboard (a different origin) posts to /agents/rank/run, so the
    # browser needs CORS. Origins are overridable via API_CORS_ORIGINS (comma-sep).
    # Default is "*" (allow any origin) because this is a read-only demo API served
    # on a per-deploy Railway domain the frontend can't know at build time — a
    # localhost-only default silently blocks the deployed dashboard in the browser
    # while server-side fetches still work. "*" is valid here precisely because
    # allow_credentials is NOT set (no cookies/auth) — the wildcard+credentials ban
    # doesn't apply. Pin API_CORS_ORIGINS to the exact frontend domain(s) to lock down.
    import os

    from fastapi.middleware.cors import CORSMiddleware

    origins = os.environ.get("API_CORS_ORIGINS", "*").split(",")

    # Registered BEFORE CORSMiddleware on purpose: add_middleware prepends, so the
    # later-added CORS layer wraps this one. An unhandled exception would otherwise
    # be answered by Starlette's ServerErrorMiddleware OUTSIDE the CORS layer — a
    # plain 500 with no Access-Control-Allow-Origin, which the browser blocks and
    # the dashboard reports as a misleading "network error". (A bare
    # @app.exception_handler(Exception) has the same flaw: it runs on
    # ServerErrorMiddleware too.) Catching here keeps the 500 inside the CORS wrap
    # with a JSON detail the UI can actually show.
    @app.middleware("http")
    async def _cors_safe_errors(request: Any, call_next: Any) -> Any:
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 — last-resort net for route bugs
            log.exception("unhandled error on %s %s", request.method, request.url.path)
            return JSONResponse(status_code=500, content={"detail": f"internal error: {exc}"[:300]})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_session() -> Any:
        with Session(engine) as session:
            yield session

    def get_llm_client() -> LLMClient:
        """Injected client in tests; a real Anthropic client in production."""
        if llm_client is not None:
            return llm_client
        from pipeline.agents import default_client

        return default_client()  # CLAUDE_API=true -> API credits; false -> Claude plan

    # Cache-only price reader (never fetches; serves the parquet bar cache).
    price_provider = MarketDataProvider()

    @app.get("/buzz/latest")
    def get_buzz_latest(session: Session = Depends(get_session)) -> dict[str, Any]:
        """Latest buzz-z per ticker (most recent attention_daily day vs baseline),
        for the screener's buzz column/sort. Only tickers with a baseline appear."""
        baselines = {b.ticker: b for b in session.execute(select(BuzzBaseline)).scalars()}
        if not baselines:
            return {"buzz": {}}
        latest = (
            select(AttentionDaily.ticker, func.max(AttentionDaily.date).label("d"))
            .group_by(AttentionDaily.ticker)
            .subquery()
        )
        rows = session.execute(
            select(AttentionDaily.ticker, AttentionDaily.social_count).join(
                latest,
                (AttentionDaily.ticker == latest.c.ticker) & (AttentionDaily.date == latest.c.d),
            )
        ).all()
        buzz = {}
        for ticker, social in rows:
            z = buzz_z(social, baselines.get(ticker))
            if z is not None:
                buzz[ticker] = z
        return {"buzz": buzz}

    @app.get("/predictions", response_model=PredictionPage)
    def get_predictions(
        session: Session = Depends(get_session),
        ticker: str | None = None,
        config_version: str | None = None,
        status: str | None = None,
        outcome: str | None = None,
        source_class: str | None = None,
        kind: str | None = None,
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> PredictionPage:
        """The prediction ledger. Each row carries its origin-news context
        (source_class / headline / url / source) via a single LEFT JOIN to the
        companion table — no N+1 lookups. ``source_class`` filters to a LEDGER lane
        (structured|social|mixed); predictions with no resolved origin are excluded
        from a lane filter but still counted under no filter.

        ``kind`` filters real vs baseline predictions: 'real' (the actual signal),
        'baseline' (the always_up/random/momentum shadows used to benchmark it), or
        omit for both. Every real prediction is shadowed by ~3 baselines that share
        its ticker/issued_at/origin headline but carry their own direction — omitting
        the filter therefore returns each story several times with mixed directions,
        which is why the LEDGER page defaults to kind='real'."""
        if source_class is not None and source_class not in ("structured", "social", "mixed"):
            raise HTTPException(
                status_code=422, detail="source_class must be structured|social|mixed"
            )
        if kind is not None and kind not in ("real", "baseline"):
            raise HTTPException(status_code=422, detail="kind must be real|baseline")
        baseline_kinds = _baseline_configs(session)
        conds = []
        if ticker:
            conds.append(Prediction.ticker == ticker)
        if config_version:
            conds.append(Prediction.config_version == config_version)
        if status:
            conds.append(Prediction.status == status)
        if outcome:
            conds.append(Prediction.outcome == outcome)
        # real vs baseline: a baseline's config carries a 'baseline' param. Guard the
        # empty set (no baseline configs yet) so we emit no degenerate IN () clause.
        bset = list(baseline_kinds)
        if kind == "real" and bset:
            conds.append(Prediction.config_version.notin_(bset))
        elif kind == "baseline":
            conds.append(Prediction.config_version.in_(bset))
        # A lane filter needs an INNER join (only resolved rows have a source_class);
        # otherwise a LEFT join so every ledger row still renders, context or not.
        if source_class is not None:
            conds.append(PredictionContext.source_class == source_class)
            count_stmt = (
                select(func.count())
                .select_from(Prediction)
                .join(PredictionContext, PredictionContext.prediction_id == Prediction.prediction_id)
                .where(*conds)
            )
        else:
            count_stmt = select(func.count()).select_from(Prediction).where(*conds)
        count = session.execute(count_stmt).scalar_one()
        rows = session.execute(
            select(Prediction, PredictionContext)
            .outerjoin(
                PredictionContext, PredictionContext.prediction_id == Prediction.prediction_id
            )
            .where(*conds)
            .order_by(Prediction.issued_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return PredictionPage(
            count=count,
            items=[
                _prediction_out(p, ctx, baseline_kinds.get(p.config_version)) for p, ctx in rows
            ],
        )

    @app.get("/metrics", response_model=list[MetricsOut])
    def get_metrics(session: Session = Depends(get_session)) -> list[MetricsOut]:
        return [
            MetricsOut(
                config_version=m.config_version,
                total_graded=m.total_graded,
                correct=m.correct,
                incorrect=m.incorrect,
                expired=m.expired,
                hit_rate=m.hit_rate,
                coverage=m.coverage,
                precision=m.precision,
                recall=m.recall,
                mean_lead_time_days=m.mean_lead_time_days,
            )
            for m in metrics_by_config(session).values()
        ]

    # NOTE: declared before /clusters/{cluster_id} so "resolve" isn't captured as an id.
    @app.get("/clusters/resolve")
    def resolve_clusters(ids: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        """Resolve a set of cluster_ids to headline/source/date/scores — powers the
        RANK page's per-ticker evidence pulldown (the ranker cites cluster_ids as
        its aligned evidence). Preserves the requested order; skips unknown ids."""
        wanted = [i.strip() for i in ids.split(",") if i.strip()]
        if not wanted:
            return {"count": 0, "items": []}
        rows = session.execute(
            select(Cluster, RawItem, ClusterScore)
            .join(RawItem, RawItem.id == Cluster.origin_item_id)
            .outerjoin(ClusterScore, ClusterScore.cluster_id == Cluster.cluster_id)
            .where(Cluster.cluster_id.in_(wanted))
        ).all()
        by_id = {cl.cluster_id: (cl, origin, cs) for cl, origin, cs in rows}
        items = []
        for cid in wanted:  # cited order = model's evidence order
            trip = by_id.get(cid)
            if trip is None:
                continue
            cl, origin, cs = trip
            tickers = (
                session.execute(select(ClusterEntity.ticker).where(ClusterEntity.cluster_id == cid))
                .scalars()
                .all()
            )
            items.append(
                {
                    "cluster_id": cl.cluster_id,
                    "title": (origin.payload_json or {}).get("title"),
                    "source": origin.source,
                    "source_class": origin.source_class,
                    "url": origin.url,
                    "published_at": origin.published_at.isoformat(),
                    "catalyst_type": cs.catalyst_type if cs else None,
                    "event_stage": cs.event_stage if cs else None,
                    "finbert_score": (
                        round(cs.finbert_score, 3) if cs and cs.finbert_score is not None else None
                    ),
                    "materiality": (
                        round(cs.materiality, 3) if cs and cs.materiality is not None else None
                    ),
                    "high_alert": bool(cs.high_alert) if cs else False,
                    "tickers": list(tickers),
                }
            )
        return {"count": len(items), "items": items}

    @app.get("/clusters/{cluster_id}", response_model=ClusterOut)
    def get_cluster(cluster_id: str, session: Session = Depends(get_session)) -> ClusterOut:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None:
            raise HTTPException(status_code=404, detail="cluster not found")
        origin = session.get(RawItem, cluster.origin_item_id)
        score = session.get(ClusterScore, cluster_id)
        ents = (
            session.execute(select(ClusterEntity).where(ClusterEntity.cluster_id == cluster_id))
            .scalars()
            .all()
        )
        return ClusterOut(
            cluster_id=cluster.cluster_id,
            origin_item_id=cluster.origin_item_id,
            origin_source=origin.source if origin else None,
            origin_title=(origin.payload_json.get("title") if origin else None),
            origin_tier=cluster.origin_tier,
            member_count=cluster.member_count,
            finbert_score=score.finbert_score if score else None,
            lm_score=score.lm_score if score else None,
            materiality=score.materiality if score else None,
            catalyst_type=score.catalyst_type if score else None,
            event_stage=score.event_stage if score else None,
            entities=[
                ClusterEntityOut(
                    ticker=e.ticker, ticker_role=e.ticker_role, match_method=e.match_method
                )
                for e in ents
            ],
        )

    @app.get("/tickers/{ticker}/state", response_model=TickerStateOut)
    def get_ticker_state(ticker: str, session: Session = Depends(get_session)) -> TickerStateOut:
        attributed = session.execute(
            select(func.count()).select_from(ClusterEntity).where(ClusterEntity.ticker == ticker)
        ).scalar_one()
        recent = (
            session.execute(
                select(ClusterEntity.cluster_id)
                .where(ClusterEntity.ticker == ticker)
                .order_by(ClusterEntity.created_at.desc())
                .limit(10)
            )
            .scalars()
            .all()
        )
        open_preds = (
            session.execute(
                select(Prediction)
                .where(Prediction.ticker == ticker, Prediction.status == "open")
                .order_by(Prediction.issued_at.desc())
            )
            .scalars()
            .all()
        )
        return TickerStateOut(
            ticker=ticker,
            attributed_clusters=attributed,
            open_predictions=[_prediction_out(p) for p in open_preds],
            recent_clusters=list(recent),
        )

    @app.get("/events")
    async def get_events() -> StreamingResponse:
        """SSE stream of real-time events (news / fired / predictions / grades /
        ranking / deep_dive). Bridges the Redis channel + in-process bus; the
        frontend keeps polling as fallback, so a dropped stream degrades cleanly."""

        async def gen():
            yield ": connected\n\n"
            agen = event_stream()
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(anext(agen), timeout=15.0)
                        yield f"data: {json.dumps(event)}\n\n"
                    except TimeoutError:
                        yield ": ping\n\n"  # keepalive
            finally:
                await agen.aclose()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    @app.get("/tickers/{ticker}/intraday/bars")
    def get_intraday_bars(
        ticker: str, window: str = Query("1d", pattern="^(1d|1w)$")
    ) -> dict[str, Any]:
        """Real intraday candles via yfinance (1m for 1d, 5m/15m fallback; 5m for
        1w), TTL-cached per ticker on demand — never bulk. prepost bars included
        and flagged `extended`; available=false when yfinance has nothing (the UI
        falls back to last close — no synthetic bars)."""
        from pipeline.marketdata.intraday import intraday_bars

        return intraday_bars(ticker.upper(), window)

    def _trends_client() -> Any:
        """Lazily-built, process-wide Trends client — one requests.Session so the
        primed Google cookie is reused across on-demand hourly fetches."""
        c = getattr(app.state, "trends_client", None)
        if c is None:
            from pipeline.ingest.trends import GoogleTrendsClient

            c = GoogleTrendsClient()
            app.state.trends_client = c
        return c

    @app.get("/tickers/{ticker}/search-interest/hourly")
    def get_search_interest_hourly(
        ticker: str, hours: int = Query(48, ge=6, le=168)
    ) -> dict[str, Any]:
        """On-demand HOURLY Google-Trends search interest for ONE ticker — own-term
        relative 0-100 (NOT counts), TTL-cached, fail-soft. Mirrors the 1m-candles
        pattern: fetched only for the viewed ticker, never bulk-polled (the
        unofficial endpoint won't tolerate hourly polling of the whole hot set).
        source='unavailable' with empty points when the lane is off or Trends balks
        — the panel shows an honest label, never synthetic bars."""
        from pipeline.ingest.trends import hourly_interest, search_trends_enabled

        tk = ticker.upper()
        label = "relative interest (0-100, own-term)"
        if not search_trends_enabled():
            return {
                "ticker": tk,
                "source": "unavailable",
                "note": "disabled",
                "label": label,
                "points": [],
            }
        return hourly_interest(tk, client=_trends_client(), hours=hours)

    @app.get("/tickers/{ticker}/intraday/live")
    def get_intraday_live(ticker: str, hours: int = Query(24, ge=1, le=26)) -> dict[str, Any]:
        """Live per-hour mention counts from the Redis ingest counters. live=false
        (empty items) when Redis is unavailable — the panel falls back to
        client-side bucketing of /api/news."""
        counts = intraday_counts(ticker.upper(), hours=hours)
        if counts is None:
            return {"ticker": ticker.upper(), "live": False, "items": []}
        return {"ticker": ticker.upper(), "live": True, "items": counts}

    # --- live quotes: watch-set -> batched Alpaca trades -> SSE "quotes" events ---
    # The screener registers its visible tickers here (TTL'd); a background task
    # polls ONE batched latest-trades request every few seconds for the union and
    # publishes a `quotes` event on the in-process bus/SSE. No keys -> everything
    # no-ops and the UI stays on its polling prices. IEX feed: thin names may
    # simply never print — absent from the payload, never faked.
    watch: dict[str, float] = {}
    WATCH_TTL = 90.0
    PUMP_INTERVAL = 4.0

    @app.get("/live/watch")
    def live_watch(tickers: str) -> dict[str, Any]:
        """Register tickers for live-quote pushes (client re-registers periodically)."""
        now_ts = utcnow().timestamp()
        added = 0
        for t in tickers.split(","):
            t = t.strip().upper()
            if t:
                watch[t] = now_ts + WATCH_TTL
                added += 1
        return {"watching": len(watch), "registered": added, "live": alpaca_configured()}

    async def _quote_pump() -> None:
        data = AlpacaData()
        last: dict[str, float] = {}
        while True:
            await asyncio.sleep(PUMP_INTERVAL)
            # A single iteration must never kill the pump task — an unhandled
            # exception here would silently stop all live-quote SSE pushes with
            # no restart and no surfaced error. Log and keep looping.
            try:
                now_ts = utcnow().timestamp()
                for t in [t for t, exp in watch.items() if exp < now_ts]:
                    watch.pop(t, None)
                    last.pop(t, None)
                if not watch:
                    continue
                trades = await asyncio.to_thread(data.latest_trades, list(watch))
                changed = {t: q for t, q in trades.items() if last.get(t) != q["price"]}
                if changed:
                    for t, q in changed.items():
                        last[t] = q["price"]
                    publish_event("quotes", quotes=changed)
            except Exception:  # noqa: BLE001 — pump must survive transient faults
                log.warning("quote pump iteration failed (continuing)", exc_info=True)

    @app.on_event("startup")
    async def _start_quote_pump() -> None:  # pragma: no cover - needs live keys
        if alpaca_configured():
            app.state.quote_pump = asyncio.create_task(_quote_pump())

    @app.get("/screener/stats")
    def get_screener_stats(tickers: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        """Per-ticker vs-own-history stats for the screener's INSIGHT strip.

        Built from attention_daily (rollup) + buzz_baselines + search interest.
        History excludes today (partial day); ratios/z-scores are null until a ticker
        has enough observed days — honest "—", never a fabricated baseline. Shares its
        implementation with /screener/rows (pipeline.aggregate.screener.ticker_stats)."""
        wanted = {t.strip().upper() for t in tickers.split(",") if t.strip()}
        out = ticker_stats(session, wanted)
        return {"count": len(out), "stats": out}

    @app.get("/screener/rows")
    def get_screener_rows(
        session: Session = Depends(get_session),
        hours: int = Query(48, ge=6, le=168),
    ) -> dict[str, Any]:
        """The NEWS screener's row set: one row per UNIVERSE ticker (latest
        fundamentals snapshot) with >= 1 attributed cluster in the last ``hours``.

        This is the coverage the Mongo /api/news window can't surface — hundreds of
        universe names with recent attributed news, server-side aggregated in a single
        windowed pass (mentions, distinct sources, both-axis sentiment kept separate,
        catalyst context on a wider lookback, buzz_z, fundamentals, vs-own-history
        stats). Windowed + indexed so it stays fast as history grows."""
        return screener_rows(session, hours=hours)

    # --- paper-sim rails (Phase 2): status, configs, immutable trade ledger ---
    @app.get("/sim/status")
    def get_sim_status(session: Session = Depends(get_session)) -> dict[str, Any]:
        """Master switch + racing state + DRIVER LIVENESS from evidence.

        Rails ship with zero configs — one is seeded only when its hypothesis passes
        a pre-registered gate (docs/gates.md).

        ``master_enabled`` is the ``SIM_ENABLED`` env as seen by THIS API process. It
        gates ONLY the pipeline-loop's in-process sim path (run_pipeline); it does NOT
        gate the standalone ``run_sim_mockup.py --daily`` driver that is the standing
        daily trader — the driver writes trades regardless of the flag. So
        ``master_enabled: false`` while trades are flowing is expected, not a bug: the
        honest "is the sim actually trading?" signal is the shared-ledger evidence
        below. ``recently_active`` = the driver entered or managed a position in the
        last 24h (reads false overnight/weekends even if the driver is up — use
        ``last_activity_at`` for nuance)."""
        from pipeline.sim import entry_guard_status, sim_enabled

        configs = session.execute(select(SimConfig)).scalars().all()
        open_n = session.execute(
            select(func.count()).select_from(SimTrade).where(SimTrade.status == "open")
        ).scalar_one()
        closed_n = session.execute(
            select(func.count()).select_from(SimTrade).where(SimTrade.status == "closed")
        ).scalar_one()
        # Liveness evidence from the ledger the driver writes to (env-independent).
        last_entry = session.execute(select(func.max(SimTrade.entered_at))).scalar_one()
        last_exit = session.execute(select(func.max(SimTrade.exited_at))).scalar_one()
        last_activity = max([t for t in (last_entry, last_exit) if t is not None], default=None)
        try:
            last_session = session.execute(
                select(func.max(SimDailySummary.session_date))
            ).scalar_one()
        except Exception:  # noqa: BLE001 — rollup table may not exist until first EOD
            last_session = None
        recently_active = last_activity is not None and (utcnow() - last_activity) <= timedelta(
            hours=24
        )
        return {
            "master_enabled": sim_enabled(),
            "configs": len(configs),
            "configs_enabled": sum(1 for c in configs if c.enabled),
            "open_trades": open_n,
            "closed_trades": closed_n,
            "recently_active": recently_active,
            "last_entry_at": last_entry.isoformat() if last_entry else None,
            "last_exit_at": last_exit.isoformat() if last_exit else None,
            "last_activity_at": last_activity.isoformat() if last_activity else None,
            "last_session_date": last_session.isoformat() if last_session else None,
            # Entry loss guards (2026-07-28): caps + today's realized $ + who's halted
            # from OPENING new positions. Exits/flatten are never gated by these.
            "entry_guards": entry_guard_status(session),
        }

    @app.get("/sim/configs")
    def get_sim_configs(session: Session = Depends(get_session)) -> dict[str, Any]:
        from pipeline.sim.exitpolicy import exit_policy_ref

        rows = session.execute(select(SimConfig).order_by(SimConfig.created_at)).scalars().all()

        def _ep(params: dict[str, Any] | None) -> dict[str, Any]:
            """The config's exit policy surfaced for racing: kind, content-addressed
            ref (so an exit-only variant is visibly distinct), + any per-catalyst map."""
            p = params or {}
            single = p.get("exit_policy") or {"kind": "horizon_hold"}
            return {
                "kind": single.get("kind", "horizon_hold"),
                "ref": exit_policy_ref(single),
                "spec": single,
                "by_catalyst": p.get("exit_policy_by_catalyst"),
            }

        return {
            "count": len(rows),
            "items": [
                {
                    "config_id": c.config_id,
                    "name": c.name,
                    "created_at": c.created_at.isoformat(),
                    "enabled": c.enabled,
                    "gate_ref": c.gate_ref,
                    "params": c.params_json,
                    "exit_policy": _ep(c.params_json),
                    "notes": c.notes,
                }
                for c in rows
            ],
        }

    @app.post("/sim/configs/{config_id}/toggle")
    def toggle_sim_config(
        config_id: str, session: Session = Depends(get_session)
    ) -> dict[str, Any]:
        """Flip a config's paper-racing switch (params stay frozen — racing rule)."""
        cfg = session.get(SimConfig, config_id)
        if cfg is None:
            raise HTTPException(status_code=404, detail="sim config not found")
        cfg.enabled = not cfg.enabled
        session.commit()
        return {"config_id": cfg.config_id, "name": cfg.name, "enabled": cfg.enabled}

    @app.get("/sim/trades")
    def get_sim_trades(
        session: Session = Depends(get_session),
        status: str | None = None,
        config_id: str | None = None,
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        conds = []
        if status:
            conds.append(SimTrade.status == status)
        if config_id:
            conds.append(SimTrade.config_id == config_id)
        rows = (
            session.execute(
                select(SimTrade).where(*conds).order_by(SimTrade.entered_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
        return {
            "count": len(rows),
            "items": [
                {
                    "trade_id": t.trade_id,
                    "config_id": t.config_id,
                    "ticker": t.ticker,
                    "direction": t.direction,
                    "entered_at": t.entered_at.isoformat(),
                    "entry_price": t.entry_price,
                    "horizon_trading_days": t.horizon_trading_days,
                    "status": t.status,
                    "exited_at": t.exited_at.isoformat() if t.exited_at else None,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "gross_return": t.gross_return,
                    "net_return": t.net_return,
                    "cluster_id": t.cluster_id,
                }
                for t in rows
            ],
        }

    @app.get("/sim/daily")
    def get_sim_daily(
        session: Session = Depends(get_session),
        days: int = Query(14, ge=1, le=120),
        config_id: str | None = None,
    ) -> dict[str, Any]:
        """EOD paper report cards — the durable per-(day, config) rollup written at
        each daily flatten (P&L, trades, hit rate). Cheap to pull for report cards
        without re-scanning the immutable ledger. Returns empty (never 500s) if the
        rollup table doesn't exist yet — the daily driver creates it on first run."""
        from datetime import timedelta

        cutoff = utcnow().date() - timedelta(days=days)
        try:
            conds = [SimDailySummary.session_date >= cutoff]
            if config_id:
                conds.append(SimDailySummary.config_id == config_id)
            rows = (
                session.execute(
                    select(SimDailySummary)
                    .where(*conds)
                    .order_by(SimDailySummary.session_date.desc(), SimDailySummary.config_name)
                )
                .scalars()
                .all()
            )
        except Exception:  # noqa: BLE001 — table may not exist until the driver's first run
            return {"count": 0, "items": []}
        return {
            "count": len(rows),
            "items": [
                {
                    "session_date": r.session_date.isoformat(),
                    "config_id": r.config_id,
                    "config_name": r.config_name,
                    "trades": r.trades,
                    "open_eod": r.open_eod,
                    "wins": r.wins,
                    "losses": r.losses,
                    "hit_rate": r.hit_rate,
                    "mean_net": r.mean_net,
                    "sum_net": r.sum_net,
                    "pnl_dollars": r.pnl_dollars,
                    "spy_ref": r.spy_ref,
                    "gate_ref": r.gate_ref,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ],
        }

    # --- TRADER view (Phase 1): read-only paper account, positions, blotter ---
    # Strictly read-only toward Alpaca (GET account/positions/orders/portfolio-
    # history/clock only — no order placement/cancel anywhere). Keys never reach
    # the browser; the PaperAccountReader fronts a ~10s TTL cache so many viewers
    # collapse to one upstream call. No keys -> {configured:false} so the deployed
    # site renders a "connect Alpaca keys" empty state instead of crashing.
    from pipeline.api import trader as _trader

    def _reader() -> Any:
        return paper_reader()

    @app.get("/trader/account")
    def get_trader_account() -> dict[str, Any]:
        """Portfolio header: equity, cash, buying power, day P&L + market clock."""
        reader = _reader()
        if reader is None:
            return {"configured": False}
        try:
            return {"available": True, **_trader.account_view(reader.account(), reader.clock())}
        except Exception:  # noqa: BLE001 — vendor hiccup must not 500 the header
            log.warning("trader account fetch failed", exc_info=True)
            return {"configured": True, "available": False}

    @app.get("/trader/portfolio/history")
    def get_trader_portfolio_history(
        period: str = Query("1M"),
        timeframe: str = Query("1D"),
        extended_hours: bool = Query(True),
    ) -> dict[str, Any]:
        """Equity curve from Alpaca's portfolio/history endpoint. ``period`` e.g.
        1D/1W/1M/3M/1A/all; ``timeframe`` e.g. 1Min/5Min/15Min/1H/1D."""
        reader = _reader()
        if reader is None:
            return {"configured": False, "points": []}
        try:
            hist = reader.portfolio_history(
                period=period, timeframe=timeframe, extended_hours=extended_hours
            )
            return {"available": True, **_trader.portfolio_history_view(hist)}
        except Exception:  # noqa: BLE001
            log.warning("trader portfolio history fetch failed", exc_info=True)
            return {"configured": True, "available": False, "points": []}

    @app.get("/trader/positions")
    def get_trader_positions(session: Session = Depends(get_session)) -> dict[str, Any]:
        """Open positions with unrealized P&L + provenance (config + catalyst)."""
        reader = _reader()
        if reader is None:
            return {"configured": False, "count": 0, "items": []}
        try:
            return {"available": True, **_trader.positions_view(reader.positions(), session)}
        except Exception:  # noqa: BLE001
            log.warning("trader positions fetch failed", exc_info=True)
            return {"configured": True, "available": False, "count": 0, "items": []}

    @app.get("/trader/blotter")
    def get_trader_blotter(
        session: Session = Depends(get_session),
        scope: str = Query("closed"),
        config_id: str | None = None,
        today_et: str | None = None,
        limit: int = Query(500, ge=1, le=500),
    ) -> dict[str, Any]:
        """Trade blotter: fills grouped into round-trips (entry->exit) with realized
        P&L, each joined back to sim_trades for config + catalyst-headline
        provenance. scope=closed|open|today|all; filter by config_id."""
        if scope not in ("closed", "open", "today", "all"):
            raise HTTPException(status_code=422, detail="scope must be closed|open|today|all")
        reader = _reader()
        if reader is None:
            return {"configured": False, "scope": scope, "count": 0, "items": []}
        try:
            orders = [] if scope == "open" else reader.orders(status="all", limit=limit)
            positions = reader.positions() if scope == "open" else []
            return {
                "available": True,
                **_trader.blotter_view(
                    orders, positions, session,
                    scope=scope, config_id=config_id, today_et=today_et,
                ),
            }
        except Exception:  # noqa: BLE001
            log.warning("trader blotter fetch failed", exc_info=True)
            return {"configured": True, "available": False, "scope": scope, "count": 0, "items": []}

    @app.get("/trader/calendar")
    def get_trader_calendar(
        session: Session = Depends(get_session),
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Per-day realized P&L for a P&L calendar (Phase 2). start/end (YYYY-MM-DD,
        ET) bound the Alpaca order pull; days are bucketed by round-trip exit."""
        reader = _reader()
        if reader is None:
            return {"configured": False, "days": {}}
        try:
            orders = reader.orders(status="all", limit=500, after=start, until=end)
            return {"available": True, **_trader.calendar_view(orders, session)}
        except Exception:  # noqa: BLE001
            log.warning("trader calendar fetch failed", exc_info=True)
            return {"configured": True, "available": False, "days": {}}

    @app.get("/trader/day")
    def get_trader_day(
        date: str,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """One day's detail: this account's round-trips exited that day + any EOD
        report cards from sim_daily_summary (flagged prior_account honestly)."""
        reader = _reader()
        if reader is None:
            return {"configured": False, "date": date, "round_trips": [], "report_cards": []}
        try:
            acct = reader.account()
            inception = _trader.account_view(acct, None).get("inception_date")
            orders = reader.orders(status="all", limit=500)
            return {
                "available": True,
                **_trader.day_view(orders, session, date=date, account_inception=inception),
            }
        except Exception:  # noqa: BLE001
            log.warning("trader day fetch failed", exc_info=True)
            return {
                "configured": True,
                "available": False,
                "date": date,
                "round_trips": [],
                "report_cards": [],
            }

    @app.get("/trader/markers/{ticker}")
    def get_trader_markers(
        ticker: str, session: Session = Depends(get_session)
    ) -> dict[str, Any]:
        """Entry/exit fills for ONE ticker, for markers on the ticker DAILY candle
        chart (aligned by ET date). Empty when unconfigured or never traded."""
        reader = _reader()
        if reader is None:
            return {"configured": False, "ticker": ticker.upper(), "markers": []}
        try:
            orders = reader.orders(status="all", limit=500)
            return {"available": True, **_trader.markers_view(orders, ticker, session)}
        except Exception:  # noqa: BLE001
            log.warning("trader markers fetch failed", exc_info=True)
            return {"configured": True, "available": False, "ticker": ticker.upper(), "markers": []}

    @app.get("/trader/overlay/{ticker}")
    def get_trader_overlay(
        ticker: str,
        session: Session = Depends(get_session),
        hours: int = Query(13, ge=1, le=168),
        today_et: str | None = None,
    ) -> dict[str, Any]:
        """Live-chart overlay for ONE ticker (Phase-follow-up): EXACT intraday fill
        markers snapped to the SAME 1-min bar grid /sim/bars serves (so alignment
        is authoritative, not eyeballed) + the intent layer (entry price line,
        flatten cutoff, signal-fired time, horizon-end, ADVISORY vol_stop). The
        bar grid is AlpacaData.cached_minute_bars — the single source of truth the
        live chart also renders — so marker epochs need no ET reshift."""
        tk = ticker.upper()
        empty = {
            "ticker": tk,
            "fill_markers": [],
            "alignment": {"checked": 0, "aligned": 0, "misaligned": 0},
            "entry_lines": [],
            "advisory": [],
            "flatten": None,
            "signal": None,
            "horizon": None,
        }
        reader = _reader()
        if reader is None:
            return {"configured": False, **empty}
        try:
            orders = reader.orders(status="all", limit=500)
            positions = reader.positions()
            clock = reader.clock()
            # SAME source /sim/bars uses — one source of truth for the bar grid.
            bars = AlpacaData().cached_minute_bars(tk, lookback_hours=hours) if alpaca_configured() else []
            daily = price_provider.cached_bars(tk, days=60)
            return {
                "available": True,
                **_trader.overlay_view(
                    tk, orders, positions, bars, clock, session, daily, today_et=today_et
                ),
            }
        except Exception:  # noqa: BLE001
            log.warning("trader overlay fetch failed", exc_info=True)
            return {"configured": True, "available": False, **empty}

    # --- TRADER watchlist lane (Phase 3): pinned tickers in OUR DB ------------
    # View/stage only. Pins live in our watchlist_pins table (not Alpaca's) so
    # each wires into the local armed-state / buzz / catalyst machinery. These
    # endpoints write ONLY to our DB and never touch Alpaca or place an order.
    from pipeline.api import watchlist as _watchlist

    @app.get("/trader/watchlist")
    def get_trader_watchlist(session: Session = Depends(get_session)) -> dict[str, Any]:
        """Pinned tickers enriched with armed/scheduled catalyst state, buzz z,
        latest premarket move, and most-recent catalyst headline."""
        return _watchlist.watchlist_view(session)

    @app.post("/trader/watchlist")
    def pin_trader_watchlist(
        body: WatchlistPinRequest, session: Session = Depends(get_session)
    ) -> dict[str, Any]:
        """Pin a ticker (idempotent; re-pin updates the note). Writes our DB only."""
        try:
            pin = _watchlist.add_pin(session, body.ticker, body.note)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ticker": pin.ticker, "note": pin.note, "created_at": pin.created_at.isoformat()}

    @app.delete("/trader/watchlist/{ticker}")
    def unpin_trader_watchlist(
        ticker: str, session: Session = Depends(get_session)
    ) -> dict[str, Any]:
        """Unpin a ticker. Writes our DB only."""
        removed = _watchlist.remove_pin(session, ticker)
        if not removed:
            raise HTTPException(status_code=404, detail="ticker not pinned")
        return {"ticker": ticker.upper(), "removed": True}

    @app.get("/tickers/{ticker}/sim/bars")
    def get_sim_bars(ticker: str, hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
        """Minute bars incl. pre/post-market from ALPACA (IEX feed), cache-backed —
        the trading sim's mark source (Phase 1). Distinct from the UI's
        /intraday/bars (yfinance): the sim needs Alpaca's premarket truth and an
        hours window. live=false with empty items when keys aren't configured."""
        t = ticker.upper()
        if not alpaca_configured():
            return {"ticker": t, "live": False, "source": None, "items": []}
        try:
            bars = AlpacaData().cached_minute_bars(t, lookback_hours=hours)
        except Exception:  # noqa: BLE001 — data-vendor hiccup must not 500 the panel
            return {"ticker": t, "live": False, "source": "alpaca-iex", "items": []}
        return {"ticker": t, "live": True, "source": "alpaca-iex", "items": bars}

    @app.get("/health", response_model=HealthOut)
    def get_health(session: Session = Depends(get_session)) -> HealthOut:
        now = utcnow()
        last = session.execute(select(func.max(RawItem.ingested_at))).scalar_one()
        per_class = {
            cls: session.execute(
                select(func.max(RawItem.ingested_at)).where(RawItem.source_class == cls)
            ).scalar_one()
            for cls in ("structured", "social")
        }
        from pipeline.ingest.firehose import DEFAULT_STATUS_PATH, liveness, read_status

        return HealthOut(
            now=now,
            raw_items=session.execute(select(func.count()).select_from(RawItem)).scalar_one(),
            clusters=session.execute(select(func.count()).select_from(Cluster)).scalar_one(),
            predictions=session.execute(select(func.count()).select_from(Prediction)).scalar_one(),
            last_ingested_at=last,
            staleness_seconds=((now - last).total_seconds() if last else None),
            per_source_class=per_class,
            firehose=liveness(read_status(DEFAULT_STATUS_PATH)),
        )

    # --- signal lab (5c.4): clean-only + holdout-excluded + backfill-excluded by default ---
    @app.get("/lab/ic")
    def get_lab_ic(
        session: Session = Depends(get_session),
        feature: str = "finbert_score",
        catalyst_type: str | None = None,
        cap_bucket: str | None = None,
        include_backfill: bool = False,
        include_holdout: bool = False,
    ) -> dict[str, Any]:
        rows = load_lab_rows(
            session,
            catalyst_type=catalyst_type,
            cap_bucket=cap_bucket,
            include_backfill=include_backfill,
            include_holdout=include_holdout,
        )
        return {
            "n": len(rows),
            "ic": [spearman_ic(rows, h, feature) for h in (1, 2, 3, 5, 10)],
            "quintile_spread": [quintile_spread(rows, h, feature) for h in (1, 2, 3, 5, 10)],
        }

    @app.get("/lab/car")
    def get_lab_car(
        session: Session = Depends(get_session),
        feature: str = "finbert_score",
        catalyst_type: str | None = None,
        include_backfill: bool = False,
        include_holdout: bool = False,
    ) -> dict[str, Any]:
        rows = load_lab_rows(
            session,
            catalyst_type=catalyst_type,
            include_backfill=include_backfill,
            include_holdout=include_holdout,
        )
        return car_curves(rows, feature)

    @app.get("/lab/observations/open")
    def get_lab_open(
        session: Session = Depends(get_session),
        limit: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        obs = (
            session.execute(
                select(SignalObservation)
                .where(SignalObservation.status == "open")
                .order_by(SignalObservation.t0.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return {
            "count": len(obs),
            "items": [
                {
                    "observation_id": o.observation_id,
                    "ticker": o.ticker,
                    "t0": o.t0.isoformat(),
                    "entry_price_date": o.entry_price_date.isoformat()
                    if o.entry_price_date
                    else None,
                    "running_car": o.marks_json,
                    "backfill": o.backfill,
                }
                for o in obs
            ],
        }

    # --- catalyst panel + presets (5b.4) ---
    @app.get("/catalysts/fired")
    def get_catalysts_fired(
        session: Session = Depends(get_session),
        limit: int = Query(50, ge=1, le=500),
        window_days: int = Query(7, ge=1, le=30),
        order: str = Query("rank", pattern="^(recent|rank)$"),
    ) -> dict[str, Any]:
        """Fired catalysts over the last ``window_days`` (default 1 week). ``order``
        = ``rank`` (materiality×recency, default) or ``recent`` (newest-first — what
        the 1-week panel requests so the whole window is browsable)."""
        items = fired_panel(session, limit=limit, window_days=window_days, order=order)
        return {"count": len(items), "items": items, "window_days": window_days, "order": order}

    @app.get("/catalysts/scheduled")
    def get_catalysts_scheduled(
        session: Session = Depends(get_session), limit: int = Query(100, ge=1, le=500)
    ) -> dict[str, Any]:
        items = scheduled_panel(session, limit=limit)
        return {"count": len(items), "items": items}

    @app.get("/catalysts/premarket")
    def get_catalysts_premarket(
        live: int = Query(0, ge=0, le=1), session: Session = Depends(get_session)
    ) -> dict[str, Any]:
        """PMR: today's frozen premarket panel (default), or a live recompute
        (?live=1) while the premarket window is open. Read-only; the frozen
        snapshot is the auditable artifact, live is a freshness convenience."""
        now = utcnow()
        now_et = now.astimezone(PMR_ET)
        today_et = now_et.date()
        in_premarket = now_et.weekday() < 5 and now_et.time() < PMR_OPEN_ET

        if live and in_premarket:
            try:
                from pipeline.marketdata import TradingCalendar

                spy = price_provider.get_benchmark_bars(today_et - timedelta(days=45), today_et)
                if not spy.empty:
                    from dataclasses import asdict

                    from pipeline.panel import premarket_window

                    cal = TradingCalendar.from_bars(spy)
                    start, end = premarket_window(cal, now)
                    rows = [asdict(r) for r in premarket_panel(session, cal, now)]
                    return {
                        "available": True,
                        "live": True,
                        "session_date": today_et.isoformat(),
                        "computed_at": now.isoformat(),
                        "window": {"start": start.isoformat(), "end": end.isoformat()},
                        "stale": False,
                        "count": len(rows),
                        "rows": rows,
                        "graded": False,
                        "outcomes": None,
                        "summary": None,
                    }
            except Exception as exc:  # noqa: BLE001 — live is best-effort, stored is truth
                log.warning("premarket live recompute failed, serving stored: %s", exc)

        panel = session.execute(
            select(PremarketPanel).order_by(PremarketPanel.session_date.desc()).limit(1)
        ).scalar_one_or_none()
        if panel is None:
            return {"available": False, "live": False, "rows": [], "count": 0}
        return {
            "available": True,
            "live": False,
            "session_date": panel.session_date.isoformat(),
            "computed_at": panel.computed_at.isoformat(),
            "window": {
                "start": panel.window_start.isoformat(),
                "end": panel.window_end.isoformat(),
            },
            # stale = this is not today's session panel (weekend/holiday mornings
            # legitimately serve Friday's panel — the flag lets the UI say so).
            "stale": panel.session_date != today_et,
            "count": len(panel.rows_json or []),
            "rows": panel.rows_json or [],
            "graded": panel.graded_at is not None,
            "outcomes": panel.outcomes_json,
            "summary": panel.summary_json,
        }

    # --- extended-session tracker: per-day pre/regular/post price behavior --------
    @app.get("/extended/movers")
    def get_extended_movers(
        session: Session = Depends(get_session),
        date: str | None = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        """Premarket movers for a day (default: today ET) — the tracked set's rows
        that had a premarket print, ranked by |premarket move|, each carrying its
        trailing premarket streak ('Nth straight premarket gain'). Names with no
        extended prints are simply absent (honest coverage, never fabricated)."""
        from datetime import date as _date

        from pipeline.common.models import ExtendedSessionDaily
        from pipeline.marketdata.extended import ET as _EXT_ET
        from pipeline.marketdata.extended import (
            available_extended_dates,
            date_label,
            extended_movers,
        )

        if date:
            try:
                d = _date.fromisoformat(date)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc
        else:
            # Default to today ET; if today hasn't logged yet, fall back to the most
            # recent session that has data (the response's `date` says which) so the
            # panel shows the latest premarket movers instead of a blank today.
            d = utcnow().astimezone(_EXT_ET).date()
            has_today = session.execute(
                select(func.count())
                .select_from(ExtendedSessionDaily)
                .where(ExtendedSessionDaily.session_date == d)
                .where(ExtendedSessionDaily.pm_pct.is_not(None))
            ).scalar_one()
            if not has_today:
                latest = session.execute(
                    select(func.max(ExtendedSessionDaily.session_date)).where(
                        ExtendedSessionDaily.pm_pct.is_not(None)
                    )
                ).scalar_one()
                if latest is not None:
                    d = latest
        out = extended_movers(session, d, limit=limit)
        # the navigable set (sessions with premarket movers), weekday-labelled so the
        # frontend's date selector doesn't guess which dates have data.
        out["available_dates"] = [
            {"date": ad.isoformat(), "label": date_label(ad)}
            for ad in available_extended_dates(session)
        ]
        return out

    @app.get("/tickers/{ticker}/extended")
    def get_ticker_extended(
        ticker: str,
        session: Session = Depends(get_session),
        days: int = Query(30, ge=1, le=180),
    ) -> dict[str, Any]:
        """A ticker's extended-session history (newest first) + its current
        premarket streak — for the detail page's PREMARKET/EXTENDED strip."""
        from pipeline.marketdata.extended import extended_history

        return extended_history(session, ticker, days=days)

    # --- news archive: browse a past day's newsfeed from the raw_items plane -------
    @app.get("/api/news")
    def get_api_news(
        session: Session = Depends(get_session),
        limit: int = Query(200, ge=1, le=1000),
        source_type: str | None = None,
        ticker: str | None = None,
    ) -> dict[str, Any]:
        """LIVE news feed served straight from raw_items — the deploy replacement for
        the external Mongo middleware the frontend used to call. Newest-first, cluster-
        attributed (tickers + sentiment), in the frontend's NewsItem shape. ``source_type``
        is the coarse tape tag (rss|sec|fda|social); ``ticker`` narrows to one symbol."""
        if source_type is not None and source_type not in (
            "rss",
            "sec",
            "fda",
            "social",
            "structured",
        ):
            raise HTTPException(
                status_code=422, detail="source_type must be rss|sec|fda|social|structured"
            )
        from pipeline.aggregate.news import live_news

        return live_news(session, source_type=source_type, ticker=ticker, limit=limit)

    @app.get("/news/dates")
    def get_news_dates(session: Session = Depends(get_session)) -> dict[str, Any]:
        """ET dates that have news (newest first, weekday-labelled) — the LIVE tape's
        archive calendar. Bounded to the recent window; gap days are absent."""
        from pipeline.aggregate.news import date_label, news_archive_dates

        dates = news_archive_dates(session)
        return {
            "count": len(dates),
            "dates": [{"date": d.isoformat(), "label": date_label(d)} for d in dates],
        }

    @app.get("/news/archive")
    def get_news_archive(
        date: str,
        session: Session = Depends(get_session),
        lane: str | None = None,
        ticker: str | None = None,
        source: str | None = None,
        q: str | None = None,
        limit: int = Query(300, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """One ET day's newsfeed from raw_items (full history, attributed), newest
        first — the archive the LIVE tape switches to when a past date is picked.
        Paginated; lane/ticker/source/q filters mirror the live tape, applied
        server-side so `count` is the honest filtered total for the day."""
        from datetime import date as _date

        from pipeline.aggregate.news import news_archive

        try:
            d = _date.fromisoformat(date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc
        if lane is not None and lane not in ("structured", "social"):
            raise HTTPException(status_code=422, detail="lane must be structured|social")
        return news_archive(
            session, day=d, lane=lane, ticker=ticker, source=source, q=q, limit=limit, offset=offset
        )

    @app.get("/presets")
    def get_presets() -> dict[str, Any]:
        presets = load_presets()
        return {
            "count": len(presets),
            "presets": {name: compile_preset(p) for name, p in presets.items()},
        }

    @app.get("/screener")
    def get_screener(
        preset: str,
        session: Session = Depends(get_session),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        presets = load_presets()
        if preset not in presets:
            raise HTTPException(status_code=404, detail=f"unknown preset: {preset}")
        filter_obj = compile_preset(presets[preset])
        items = screen(session, filter_obj, limit=limit)
        return {"preset": preset, "filter": filter_obj, "count": len(items), "items": items}

    # --- UNIVERSE screener (Finviz-style, server-side over fundamentals_snapshots) ---
    _UNIVERSE_SORTS = {
        "market_cap": FundamentalsSnapshot.market_cap,
        "price": FundamentalsSnapshot.price,
        "change_pct": FundamentalsSnapshot.change_pct,
        "avg_volume": FundamentalsSnapshot.avg_volume,
        "short_float": FundamentalsSnapshot.short_float,
        "inst_own": FundamentalsSnapshot.inst_own,
        "beta": FundamentalsSnapshot.beta,
        "ticker": FundamentalsSnapshot.ticker,
    }

    @app.get("/universe/facets")
    def get_universe_facets(session: Session = Depends(get_session)) -> dict[str, Any]:
        """Distinct sectors/industries (with counts) for the filter dropdowns."""
        latest = select(func.max(FundamentalsSnapshot.as_of)).scalar_subquery()
        total = session.execute(
            select(func.count())
            .select_from(FundamentalsSnapshot)
            .where(FundamentalsSnapshot.as_of == latest)
        ).scalar_one()
        sectors = session.execute(
            select(FundamentalsSnapshot.sector, func.count())
            .where(FundamentalsSnapshot.as_of == latest, FundamentalsSnapshot.sector.isnot(None))
            .group_by(FundamentalsSnapshot.sector)
            .order_by(FundamentalsSnapshot.sector)
        ).all()
        industries = (
            session.execute(
                select(FundamentalsSnapshot.industry)
                .where(
                    FundamentalsSnapshot.as_of == latest,
                    FundamentalsSnapshot.industry.isnot(None),
                )
                .distinct()
                .order_by(FundamentalsSnapshot.industry)
            )
            .scalars()
            .all()
        )
        as_of = session.execute(select(func.max(FundamentalsSnapshot.as_of))).scalar()
        return {
            "as_of": as_of.isoformat() if as_of else None,
            "universe": total,
            "sectors": [{"name": s, "count": n} for s, n in sectors],
            "industries": list(industries),
        }

    @app.get("/universe/screen")
    def get_universe_screen(
        session: Session = Depends(get_session),
        q: str | None = None,
        sector: str | None = None,
        industry: str | None = None,
        mcap_min: float | None = None,
        mcap_max: float | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        avgvol_min: float | None = None,
        short_min: float | None = None,
        short_max: float | None = None,
        inst_min: float | None = None,
        insider_min: float | None = None,
        beta_min: float | None = None,
        beta_max: float | None = None,
        change_min: float | None = None,
        change_max: float | None = None,
        has_signal: bool = False,
        earnings_within: int | None = None,
        sort: str = "market_cap",
        order: str = "desc",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """Filter the tradeable universe server-side over the latest fundamentals
        snapshot; overlay our own signal (predictions) and next earnings date."""
        F = FundamentalsSnapshot
        latest = select(func.max(F.as_of)).scalar_subquery()
        conds = [F.as_of == latest]
        # Ticker/name search: symbol prefix OR company-name substring, so any
        # ticker in the snapshot is findable regardless of the other filters.
        needle = (q or "").strip()
        if needle:
            conds.append(
                or_(
                    F.ticker.like(f"{needle.upper()}%"),
                    F.ticker.in_(
                        select(Entity.ticker).where(Entity.canonical_name.ilike(f"%{needle}%"))
                    ),
                )
            )
        if sector:
            conds.append(F.sector == sector)
        if industry:
            conds.append(F.industry == industry)
        if mcap_min is not None:
            conds.append(F.market_cap >= mcap_min)
        if mcap_max is not None:
            conds.append(F.market_cap <= mcap_max)
        if price_min is not None:
            conds.append(F.price >= price_min)
        if price_max is not None:
            conds.append(F.price <= price_max)
        if avgvol_min is not None:
            conds.append(F.avg_volume >= avgvol_min)
        if short_min is not None:
            conds.append(F.short_float >= short_min)
        if short_max is not None:
            conds.append(F.short_float <= short_max)
        if inst_min is not None:
            conds.append(F.inst_own >= inst_min)
        if insider_min is not None:
            conds.append(F.insider_own >= insider_min)
        if beta_min is not None:
            conds.append(F.beta >= beta_min)
        if beta_max is not None:
            conds.append(F.beta <= beta_max)
        if change_min is not None:
            conds.append(F.change_pct >= change_min)
        if change_max is not None:
            conds.append(F.change_pct <= change_max)
        if has_signal:
            conds.append(F.ticker.in_(select(Prediction.ticker).distinct()))
        if earnings_within is not None:
            cutoff = utcnow().date() + timedelta(days=earnings_within)
            conds.append(
                F.ticker.in_(
                    select(ScheduledEvent.ticker).where(
                        ScheduledEvent.status == "upcoming",
                        ScheduledEvent.event_date <= cutoff,
                    )
                )
            )

        total = session.execute(select(func.count()).select_from(F).where(*conds)).scalar_one()

        sort_col = _UNIVERSE_SORTS.get(sort, F.market_cap)
        sort_col = sort_col.desc() if order == "desc" else sort_col.asc()
        order_cols = [sort_col]
        if needle:
            # An exact symbol match outranks the chosen sort (searching "A"
            # must surface A itself first, not the largest name containing "a").
            order_cols.insert(0, case((F.ticker == needle.upper(), 0), else_=1))
        rows = session.execute(
            select(F, Entity.canonical_name)
            .outerjoin(Entity, Entity.ticker == F.ticker)
            .where(*conds)
            .order_by(*order_cols)
            .limit(limit)
            .offset(offset)
        ).all()
        tickers = [f.ticker for f, _ in rows]

        # Overlays for the page: latest live signal + next upcoming earnings.
        signal: dict[str, Any] = {}
        for p in session.execute(
            select(Prediction)
            .where(Prediction.ticker.in_(tickers))
            .order_by(Prediction.issued_at.desc())
        ).scalars():
            signal.setdefault(p.ticker, {"direction": p.direction, "confidence": p.confidence})
        earnings: dict[str, str] = {}
        for ev in session.execute(
            select(ScheduledEvent)
            .where(
                ScheduledEvent.ticker.in_(tickers),
                ScheduledEvent.status == "upcoming",
                ScheduledEvent.catalyst_type == "earnings_results",
            )
            .order_by(ScheduledEvent.event_date)
        ).scalars():
            earnings.setdefault(ev.ticker, ev.event_date.isoformat())

        items = [
            {
                "ticker": f.ticker,
                "name": name,
                "sector": f.sector,
                "industry": f.industry,
                "market_cap": f.market_cap,
                "price": f.price,
                "change_pct": f.change_pct,
                "avg_volume": f.avg_volume,
                "short_float": f.short_float,
                "inst_own": f.inst_own,
                "insider_own": f.insider_own,
                "beta": f.beta,
                "signal": signal.get(f.ticker),
                "next_earnings": earnings.get(f.ticker),
            }
            for f, name in rows
        ]
        return {"count": total, "limit": limit, "offset": offset, "items": items}

    @app.get("/tickers/{ticker}/series")
    def get_ticker_series(
        ticker: str,
        session: Session = Depends(get_session),
        days: int = Query(120, ge=5, le=730),
    ) -> dict[str, Any]:
        """Chart series for one ticker: price bars (parquet cache) + the daily
        attention series (news volume, mean sentiment, buzz-z). Price is the floor
        that every ticker has; attention/buzz appear only where there is data."""
        ticker = ticker.upper()
        cutoff = utcnow().date() - timedelta(days=days)
        baseline = session.get(BuzzBaseline, ticker)
        rows = (
            session.execute(
                select(AttentionDaily)
                .where(AttentionDaily.ticker == ticker, AttentionDaily.date >= cutoff)
                .order_by(AttentionDaily.date)
            )
            .scalars()
            .all()
        )
        attention = [
            {
                "date": a.date.isoformat(),
                "struct": a.struct_count,
                "social": a.social_count,
                "sentiment": round(a.sentiment_mean, 3) if a.sentiment_mean is not None else None,
                "buzz_z": buzz_z(a.social_count, baseline),
            }
            for a in rows
        ]
        return {
            "ticker": ticker,
            "baseline": (
                {
                    "mean": baseline.mean,
                    "std": baseline.std,
                    "n_days": baseline.n_days,
                    "source": baseline.source,
                }
                if baseline
                else None
            ),
            "price": price_provider.cached_bars(ticker, days=days),
            "attention": attention,
        }

    @app.get("/tickers/{ticker}/clusters")
    def get_ticker_clusters(
        ticker: str,
        session: Session = Depends(get_session),
        limit: int = Query(30, ge=1, le=100),
    ) -> dict[str, Any]:
        """Recent news clusters attributed to a ticker (for the ticker panel's
        associated-news list) — title, source, catalyst, sentiment, materiality."""
        ticker = ticker.upper()
        rows = session.execute(
            select(Cluster, RawItem, ClusterScore)
            .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
            .join(RawItem, RawItem.id == Cluster.origin_item_id)
            .outerjoin(ClusterScore, ClusterScore.cluster_id == Cluster.cluster_id)
            .where(ClusterEntity.ticker == ticker)
            .order_by(RawItem.published_at.desc())
            .limit(limit)
        ).all()
        items = [
            {
                "cluster_id": cl.cluster_id,
                "title": (origin.payload_json or {}).get("title"),
                "source": origin.source,
                "source_class": origin.source_class,
                "url": origin.url,
                "published_at": origin.published_at.isoformat(),
                "member_count": cl.member_count,
                "catalyst_type": cs.catalyst_type if cs else None,
                "finbert_score": (
                    round(cs.finbert_score, 3) if cs and cs.finbert_score is not None else None
                ),
                "materiality": round(cs.materiality, 3) if cs else None,
                "high_alert": bool(cs.high_alert) if cs else False,
                # First-scored time = when the system made the call (stable across re-scores).
                "called_at": cs.created_at.isoformat() if cs and cs.created_at else None,
            }
            for cl, origin, cs in rows
        ]
        return {"ticker": ticker, "count": len(items), "items": items}

    @app.get("/fundamentals")
    def get_fundamentals(tickers: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        """Latest fundamentals (sector/industry/market cap/avg volume/short/beta)
        for a comma-separated ticker list — the overlay that brings UNIVERSE-grade
        filters (market cap, sector, industry, volume) onto the news screener."""
        wanted = {t.strip().upper() for t in tickers.split(",") if t.strip()}
        if not wanted:
            return {"count": 0, "fundamentals": {}}
        latest = select(func.max(FundamentalsSnapshot.as_of)).scalar_subquery()
        rows = session.execute(
            select(FundamentalsSnapshot, Entity.canonical_name)
            .outerjoin(Entity, Entity.ticker == FundamentalsSnapshot.ticker)
            .where(
                FundamentalsSnapshot.as_of == latest,
                FundamentalsSnapshot.ticker.in_(wanted),
            )
        ).all()
        out = {
            f.ticker: {
                "name": name,
                "sector": f.sector,
                "industry": f.industry,
                "market_cap": f.market_cap,
                "avg_volume": f.avg_volume,
                "short_float": f.short_float,
                "beta": f.beta,
            }
            for f, name in rows
        }
        return {"count": len(out), "fundamentals": out}

    @app.get("/marketdata/prices")
    def get_prices(tickers: str) -> dict[str, Any]:
        """Latest close / %chg / vol-vs-avg for a comma-separated ticker list,
        served ONLY from the parquet bar cache (no network fetch). Uncached
        tickers are simply absent from the map (the UI renders them as '—')."""
        wanted = [t.strip().upper() for t in tickers.split(",") if t.strip()][:500]
        prices = {}
        for t in wanted:
            q = price_provider.cached_quote(t)
            if q is not None:
                prices[t] = q
        return {"count": len(prices), "prices": prices}

    # --- agent layer (Phase 7) — the ranker PROPOSES; it never writes config/ledger (I6) ---
    @app.get("/agents/models")
    def get_agent_models() -> dict[str, Any]:
        """Selectable models for the force-run control (frontend dropdown)."""
        return {"models": sorted(ALLOWED_MODELS), "default": DEFAULT_RANKER_MODEL}

    @app.post("/agents/rank/run", response_model=RankingRunOut)
    def post_rank_run(
        body: RankRunRequest,
        session: Session = Depends(get_session),
        client: LLMClient = Depends(get_llm_client),
    ) -> RankingRunOut:
        """Force-run an AI ranking with operator-chosen model + timeframe (manual trigger)."""
        try:
            model = resolve_model(body.model)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        cfg = get_or_create_config(session)
        filter_spec = build_candidate_filter(
            presets=body.presets,
            high_alert=body.high_alert,
            extreme_sentiment=body.extreme_sentiment,
        )
        try:
            run = run_ranking(
                session,
                client,
                params=cfg.params_json,
                config_version=cfg.config_version,
                filter_spec=filter_spec,
                model=model,
                horizon_trading_days=body.horizon_trading_days,
                trigger="manual",
                explicit_model=True,  # force-run = deliberate operator selection (Opus allowed)
                limit=body.limit,
            )
        except SoftCapExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        items = (
            session.execute(
                select(Ranking).where(Ranking.run_id == run.run_id).order_by(Ranking.rank)
            )
            .scalars()
            .all()
        )
        publish_event("ranking", run_id=run.run_id, status=run.status, items=len(items))
        return _ranking_run_out(run, items)

    @app.get("/agents/rankings", response_model=list[RankingRunOut])
    def get_rankings(
        session: Session = Depends(get_session), limit: int = Query(20, ge=1, le=100)
    ) -> list[RankingRunOut]:
        runs = (
            session.execute(select(RankingRun).order_by(RankingRun.created_at.desc()).limit(limit))
            .scalars()
            .all()
        )
        out = []
        for run in runs:
            items = (
                session.execute(
                    select(Ranking).where(Ranking.run_id == run.run_id).order_by(Ranking.rank)
                )
                .scalars()
                .all()
            )
            out.append(_ranking_run_out(run, items))
        return out

    @app.get("/agents/rankings/{run_id}", response_model=RankingRunOut)
    def get_ranking(run_id: str, session: Session = Depends(get_session)) -> RankingRunOut:
        run = session.get(RankingRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="ranking run not found")
        items = (
            session.execute(select(Ranking).where(Ranking.run_id == run_id).order_by(Ranking.rank))
            .scalars()
            .all()
        )
        return _ranking_run_out(run, items)

    @app.get("/agents/spend", response_model=SpendOut)
    def get_spend(session: Session = Depends(get_session)) -> SpendOut:
        now = utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        total = session.execute(
            select(func.coalesce(func.sum(LlmSpend.cost_usd), 0.0))
        ).scalar_one()
        calls = session.execute(select(func.count()).select_from(LlmSpend)).scalar_one()
        today = round(spend_since(session, day_start), 6)
        cap = default_daily_cap()
        return SpendOut(
            today_usd=today,
            total_usd=round(float(total or 0.0), 6),
            calls=calls,
            cap_usd=cap,
            pct_of_cap=round(today / cap, 4) if cap > 0 else 0.0,
        )

    @app.get("/agents/proposals")
    def get_proposals(
        session: Session = Depends(get_session), status: str | None = None
    ) -> dict[str, Any]:
        conds = [PendingChange.status == status] if status else []
        rows = (
            session.execute(
                select(PendingChange).where(*conds).order_by(PendingChange.created_at.desc())
            )
            .scalars()
            .all()
        )
        return {
            "count": len(rows),
            "items": [
                {
                    "id": c.id,
                    "created_at": c.created_at.isoformat(),
                    "status": c.status,
                    "base_config_version": c.base_config_version,
                    "patch": c.patch_json,
                    "rationale": c.rationale,
                    "report_md": c.report_md,
                    "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
                    "resolved_reason": c.resolved_reason,
                    "resulting_config_version": c.resulting_config_version,
                }
                for c in rows
            ],
        }

    # --- config panel (task 7.3): versions + human-gated approve/reject --------
    # The current config the pipeline issues under is the content-addressed v1
    # blob; approvals mint additional immutable versions off proposals (I3).
    def _current_version(session: Session) -> str:
        # Ensure the baseline exists (idempotent, content-addressed) and return its id.
        return get_or_create_config(session).config_version

    @app.get("/config/current", response_model=ConfigOut)
    def get_config_current(session: Session = Depends(get_session)) -> ConfigOut:
        """The config version the pipeline currently issues under, full params blob."""
        cfg = get_or_create_config(session)
        return ConfigOut(
            config_version=cfg.config_version,
            created_at=cfg.created_at,
            notes=cfg.notes,
            is_current=True,
            params=cfg.params_json,
        )

    @app.get("/config/versions", response_model=list[ConfigVersionOut])
    def get_config_versions(session: Session = Depends(get_session)) -> list[ConfigVersionOut]:
        """Immutable version history, newest first. Marks the current version and
        which versions were minted by approving a proposal."""
        current = _current_version(session)
        from_proposal = set(
            session.execute(
                select(PendingChange.resulting_config_version).where(
                    PendingChange.resulting_config_version.is_not(None)
                )
            ).scalars()
        )
        rows = session.execute(select(Config).order_by(Config.created_at.desc())).scalars().all()
        return [
            ConfigVersionOut(
                config_version=c.config_version,
                created_at=c.created_at,
                notes=c.notes,
                is_current=c.config_version == current,
                from_proposal=c.config_version in from_proposal,
            )
            for c in rows
        ]

    @app.get("/config/versions/{version}", response_model=ConfigOut)
    def get_config_version(version: str, session: Session = Depends(get_session)) -> ConfigOut:
        """Full immutable params blob for a specific version (history detail view)."""
        cfg = session.get(Config, version)
        if cfg is None:
            raise HTTPException(status_code=404, detail=f"config version {version} not found")
        return ConfigOut(
            config_version=cfg.config_version,
            created_at=cfg.created_at,
            notes=cfg.notes,
            is_current=cfg.config_version == _current_version(session),
            params=cfg.params_json,
        )

    @app.post("/config/proposals/{change_id}/approve", response_model=ConfigOut)
    def post_approve(
        change_id: str, body: ApproveRequest, session: Session = Depends(get_session)
    ) -> ConfigOut:
        """Human gate: apply the proposal's patch -> a NEW immutable config version.

        Calls the same pipeline.common.approval path as scripts/approve.py. Nothing
        auto-applies; this endpoint IS the human clicking approve (I3)."""
        try:
            cfg = approve_change(session, change_id, notes=body.notes)
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ConfigOut(
            config_version=cfg.config_version,
            created_at=cfg.created_at,
            notes=cfg.notes,
            is_current=cfg.config_version == _current_version(session),
            params=cfg.params_json,
        )

    @app.post("/config/proposals/{change_id}/reject")
    def post_reject(
        change_id: str, body: RejectRequest, session: Session = Depends(get_session)
    ) -> dict[str, Any]:
        """Human gate: archive a proposal with a reason (no version is minted)."""
        try:
            change = reject_change(session, change_id, reason=body.reason)
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "id": change.id,
            "status": change.status,
            "resolved_reason": change.resolved_reason,
            "resolved_at": change.resolved_at.isoformat() if change.resolved_at else None,
        }

    # --- deep dive: single-ticker AI analysis over our OWN data (task 7.4) --------
    @app.get("/tickers/{ticker}/analysis", response_model=TickerAnalysisOut | None)
    def get_ticker_analysis(
        ticker: str, session: Session = Depends(get_session)
    ) -> TickerAnalysisOut | None:
        """Latest persisted deep dive for a ticker (instant revisit; no model call).
        Returns null when the ticker has never been analyzed."""
        a = latest_analysis(session, ticker)
        return _analysis_out(a) if a is not None else None

    @app.get("/tickers/{ticker}/analyses", response_model=list[TickerAnalysisOut])
    def get_ticker_analyses(
        ticker: str,
        session: Session = Depends(get_session),
        limit: int = Query(10, ge=1, le=50),
    ) -> list[TickerAnalysisOut]:
        """Deep-dive history for a ticker (newest first)."""
        rows = (
            session.execute(
                select(TickerAnalysis)
                .where(TickerAnalysis.ticker == ticker.upper())
                .order_by(TickerAnalysis.created_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [_analysis_out(a) for a in rows]

    @app.post("/tickers/{ticker}/analyze", response_model=TickerAnalysisOut)
    def post_ticker_analyze(
        ticker: str,
        body: DeepDiveRunRequest,
        session: Session = Depends(get_session),
        client: LLMClient = Depends(get_llm_client),
    ) -> TickerAnalysisOut:
        """Run a fresh deep dive (own-data only). Rate limited to a few distinct
        tickers per rolling window (429 + Retry-After) and daily-cap guarded."""
        try:
            model = resolve_model(body.model)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        cfg = get_or_create_config(session)
        try:
            analysis = run_deep_dive(
                session,
                client,
                ticker,
                params=cfg.params_json,
                config_version=cfg.config_version,
                model=model,
                horizon_trading_days=body.horizon_trading_days,
            )
        except DeepDiveRateLimited as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except SoftCapExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        publish_event("deep_dive", ticker=ticker.upper(), status=analysis.status)
        return _analysis_out(analysis)

    @app.get("/tickers/{ticker}/analysis/rate")
    def get_analysis_rate(ticker: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        """Whether a deep-dive run for this ticker is currently allowed (for the UI
        to pre-disable the button and show a countdown)."""
        allowed, retry_after = deep_dive_rate_status(session, ticker)
        return {"allowed": allowed, "retry_after": retry_after}

    return app
