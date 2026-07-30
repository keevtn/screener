"""SQLAlchemy models for the pipeline spine (docs/ROADMAP.md task 0.2).

Invariants enforced here:
- I1: UTCDateTime rejects naive datetimes at bind time and re-attaches UTC on read
  (SQLite drops tzinfo in storage).
- I2: raw_items is append-only — ORM event hooks raise on update/delete, and SQLite
  triggers guard raw-SQL paths that bypass the ORM.
- I3: configs rows are immutable once created (hook; the versioned loader lands in 4.3).
- I4: predictions rows are immutable after issue except the grader outcome fields.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from datetime import date as date_
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AppendOnlyViolation(RuntimeError):
    """Raised on any attempt to update or delete an append-only row (I2)."""


class ImmutableRowViolation(RuntimeError):
    """Raised on illegal mutation of configs (I3) or predictions (I4)."""


class _TolerantDateResult:
    """Mixin: on READ, degrade a malformed / non-string stored value to None instead
    of raising. SQLite's DATE/DATETIME result processor calls ``date.fromisoformat`` /
    ``datetime.fromisoformat`` with only a ``value is not None`` guard — no
    ``isinstance(str)`` check — so a row that somehow holds a non-string (int/float/
    bytes) in a date column raises "fromisoformat: argument must be str" and 500s the
    whole endpoint (observed on the Railway deploy: /screener/rows, /universe/screen).
    A read-only API must not fail an entire page over one corrupt row: the bad value
    comes back None and the rest of the row still renders.

    Overriding ``result_processor`` (rather than ``process_result_value``) is required
    because the impl's processor is what raises, and it runs BEFORE process_result_value.
    The write/bind path is untouched — only reads are made defensive.
    """

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        inner = self.impl_instance.result_processor(dialect, coltype)

        def process(value: Any) -> Any:
            if inner is not None:
                try:
                    value = inner(value)
                except (TypeError, ValueError):
                    return None
            return self.process_result_value(value, dialect)

        return process


class UTCDateTime(_TolerantDateResult, sa.types.TypeDecorator):
    """Aware-UTC datetime column: naive values are rejected, reads come back aware."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected: all timestamps must be tz-aware (I1)")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class SafeDate(_TolerantDateResult, sa.types.TypeDecorator):
    """A plain date column with the same tolerant read as UTCDateTime (see
    _TolerantDateResult). Storage and binding are identical to ``sa.Date``."""

    impl = sa.Date
    cache_ok = True

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        return value


class Base(DeclarativeBase):
    pass


def raw_item_id(source: str, *, guid: str | None = None, url: str | None = None) -> str:
    """Deterministic raw_items id: sha256(source, guid|url), guid preferred (task 0.2/1.1)."""
    key = guid or url
    if not key:
        raise ValueError("raw_item_id requires a guid or url")
    return hashlib.sha256(f"{source}|{key}".encode()).hexdigest()


def params_hash(params: dict[str, Any]) -> str:
    """Stable hash of a config params blob (key-sorted JSON)."""
    import json

    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()


class RawItem(Base):
    __tablename__ = "raw_items"
    __table_args__ = (
        sa.CheckConstraint(
            "source_class IN ('structured', 'social')", name="ck_raw_items_source_class"
        ),
        # /health does WHERE source_class=? MAX(ingested_at) — composite serves both.
        sa.Index("ix_raw_items_source_class_ingested", "source_class", "ingested_at"),
    )

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    source: Mapped[str] = mapped_column(sa.String(200))
    source_class: Mapped[str] = mapped_column(sa.String(16), index=True)
    url: Mapped[str | None] = mapped_column(sa.Text)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # Indexed: /health polls MAX(ingested_at) on this (the largest table); the
    # incremental enrich watermark also filters on it. Append-only -> index only grows.
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)


class Entity(Base):
    __tablename__ = "entities"

    ticker: Mapped[str] = mapped_column(sa.String(12), primary_key=True)
    cik: Mapped[str | None] = mapped_column(sa.String(10), index=True)
    canonical_name: Mapped[str] = mapped_column(sa.Text)
    aliases_json: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    cashtag: Mapped[str | None] = mapped_column(sa.String(13))
    exchange: Mapped[str | None] = mapped_column(sa.String(20))
    active: Mapped[bool] = mapped_column(default=True)
    # New listings get a cold-start window (~30d): buzz z-scores suppressed and
    # social min-sample gates raised until it passes (task 5b.2; Phase 6 honors it).
    cold_start_until: Mapped[date_ | None] = mapped_column(SafeDate)


class Config(Base):
    __tablename__ = "configs"

    config_version: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    params_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    params_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    notes: Mapped[str | None] = mapped_column(sa.Text)


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        sa.CheckConstraint("direction IN ('bullish', 'bearish')", name="ck_predictions_direction"),
        sa.CheckConstraint("status IN ('open', 'graded')", name="ck_predictions_status"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('correct', 'incorrect', 'expired')",
            name="ck_predictions_outcome",
        ),
    )

    prediction_id: Mapped[str] = mapped_column(
        sa.String(32), primary_key=True, default=lambda: uuid4().hex
    )
    ticker: Mapped[str] = mapped_column(sa.String(12), index=True)
    direction: Mapped[str] = mapped_column(sa.String(8))
    confidence: Mapped[float] = mapped_column(sa.Float)
    horizon_trading_days: Mapped[int] = mapped_column(sa.Integer)
    threshold: Mapped[float] = mapped_column(sa.Float)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    config_version: Mapped[str] = mapped_column(sa.ForeignKey("configs.config_version"))
    evidence_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    # Indexed: /predictions?status= and the ledger badge filter scan on status.
    status: Mapped[str] = mapped_column(sa.String(8), default="open", index=True)
    outcome: Mapped[str | None] = mapped_column(sa.String(10))
    realized_adjusted_return: Mapped[float | None] = mapped_column(sa.Float)
    graded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolving_close: Mapped[date_ | None] = mapped_column(SafeDate)  # crossing close (lead time)


class FundamentalsSnapshot(Base):
    """Point-in-time fundamentals (task 0.6). Weekly-min cadence; the signal lab
    (5c) joins the nearest snapshot at-or-before an observation's t0 (I12)."""

    __tablename__ = "fundamentals_snapshots"

    ticker: Mapped[str] = mapped_column(sa.String(12), primary_key=True)
    # Indexed: every /universe/* query does MAX(as_of) then WHERE as_of==latest;
    # as_of is not the leading PK column so the composite PK can't serve it.
    as_of: Mapped[date_] = mapped_column(SafeDate, primary_key=True, index=True)
    provider: Mapped[str] = mapped_column(sa.String(32))  # provenance stamp
    market_cap: Mapped[float | None] = mapped_column(sa.Float)
    shares_float: Mapped[float | None] = mapped_column(sa.Float)
    short_float: Mapped[float | None] = mapped_column(sa.Float)
    insider_own: Mapped[float | None] = mapped_column(sa.Float)
    inst_own: Mapped[float | None] = mapped_column(sa.Float)
    avg_volume: Mapped[float | None] = mapped_column(sa.Float)
    beta: Mapped[float | None] = mapped_column(sa.Float)
    sector: Mapped[str | None] = mapped_column(sa.String(80))
    industry: Mapped[str | None] = mapped_column(sa.String(120))
    price: Mapped[float | None] = mapped_column(sa.Float)  # snapshot close (Finviz)
    change_pct: Mapped[float | None] = mapped_column(sa.Float)  # day change at snapshot
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class UniverseSnapshot(Base):
    """A dated, provider-stamped universe materialization (task 0.6).

    A provider switch or a membership diff over the config threshold lands as
    status='pending_review' (not applied to entities.active) for human sign-off,
    since a large jump poisons buzz baselines + entity stats.
    """

    __tablename__ = "universe_snapshots"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('applied', 'pending_review')", name="ck_universe_snapshots_status"
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(
        sa.String(32), primary_key=True, default=lambda: uuid4().hex
    )
    snapshot_date: Mapped[date_] = mapped_column(SafeDate, index=True)
    provider: Mapped[str] = mapped_column(sa.String(32))  # provenance stamp
    status: Mapped[str] = mapped_column(sa.String(16))
    members_json: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    diff_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    notes: Mapped[str | None] = mapped_column(sa.Text)


class Cluster(Base):
    """A story cluster: one origin item + its near-duplicate members (task 2.2).

    cluster_id == the chosen origin item's id (deterministic, so backfill upserts
    idempotently). origin = earliest published_at, source tier breaks ties (2.3).
    Scoring happens once per cluster on the origin's text (I5).
    ROADMAP-NOTE: cluster_id keys off the origin; if a later-arriving member is
    *earlier* than the current origin the id would move — fine for whole-archive
    backfill (deterministic), revisited if incremental clustering needs it.
    """

    __tablename__ = "clusters"

    cluster_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    origin_item_id: Mapped[str] = mapped_column(sa.ForeignKey("raw_items.id"), index=True)
    member_ids_json: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    origin_tier: Mapped[int | None] = mapped_column(sa.Integer)
    member_count: Mapped[int] = mapped_column(sa.Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ClusterEntity(Base):
    """A cluster→ticker attribution with a directional role (task 2.4).

    ticker_role matters for multi-party events (M&A): acquirer vs target have
    near-opposite directional implications, so role-less attribution is unsafe.
    """

    __tablename__ = "cluster_entities"
    __table_args__ = (
        sa.UniqueConstraint("cluster_id", "ticker", name="uq_cluster_entities_cluster_ticker"),
        sa.CheckConstraint(
            "ticker_role IN ('subject', 'target', 'acquirer', 'issuer', 'peer')",
            name="ck_cluster_entities_role",
        ),
        # The ticker/state panel does WHERE ticker=? ORDER BY created_at DESC;
        # the composite serves both the filter and the sort for a hot ticker.
        sa.Index("ix_cluster_entities_ticker_created", "ticker", "created_at"),
    )

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=lambda: uuid4().hex)
    cluster_id: Mapped[str] = mapped_column(sa.ForeignKey("clusters.cluster_id"), index=True)
    ticker: Mapped[str] = mapped_column(sa.String(12), index=True)
    ticker_role: Mapped[str] = mapped_column(sa.String(12), default="subject")
    match_method: Mapped[str] = mapped_column(sa.String(16))  # cashtag|name|alias|fuzzy
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class UnmappedMention(Base):
    """A ticker-bearing mention that resolution could not attribute (task 2.4).

    Feeds configs/aliases.yaml growth and the Gate 2 unmapped-rate metric.
    """

    __tablename__ = "unmapped_mentions"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=lambda: uuid4().hex)
    cluster_id: Mapped[str] = mapped_column(sa.ForeignKey("clusters.cluster_id"), index=True)
    mention: Mapped[str] = mapped_column(sa.Text)
    reason: Mapped[str] = mapped_column(sa.String(24))  # blocklist|no_match|common_word|ambiguous
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ClusterScore(Base):
    """Two-axis, cluster-scoped scores (docs/ROADMAP.md Phase 3). One row per
    cluster (I5 — scored once on the origin item's text).

    I7: model scores are stored SEPARATELY (finbert_score, lm_score) and never
    pre-blended — blend weights live in config and apply only at aggregation.
    """

    __tablename__ = "cluster_scores"
    __table_args__ = (
        sa.CheckConstraint(
            "text_kind IN ('filing', 'press_release', 'article')",
            name="ck_cluster_scores_text_kind",
        ),
    )

    cluster_id: Mapped[str] = mapped_column(sa.ForeignKey("clusters.cluster_id"), primary_key=True)
    # 3.1 sentiment axis — separate model scores (I7)
    finbert_label: Mapped[str | None] = mapped_column(sa.String(8))
    finbert_score: Mapped[float | None] = mapped_column(sa.Float)
    lm_score: Mapped[float | None] = mapped_column(sa.Float)
    # 3.3 routing metadata
    text_kind: Mapped[str] = mapped_column(sa.String(16), default="article")
    # 3.2 catalyst / materiality axis
    catalyst_type: Mapped[str | None] = mapped_column(sa.String(32), index=True)
    event_stage: Mapped[str | None] = mapped_column(sa.String(16))
    materiality: Mapped[float] = mapped_column(sa.Float, default=0.0)
    direction_hint: Mapped[str | None] = mapped_column(sa.String(20))
    high_alert: Mapped[bool] = mapped_column(default=False)
    predictive: Mapped[bool] = mapped_column(default=True)
    # 3.4 earnings-surprise guard
    reaction_dependent: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ArmedState(Base):
    """Catalyst-armed drift, the PEAD pattern (docs/ROADMAP.md task 4.5).

    A reaction_dependent cluster (earnings) arms the ticker instead of contributing
    text direction. On the first post-event session's close the market-adjusted
    reaction sets the drift direction; |reaction| ≥ config threshold emits a
    continuation prediction. Unresolved armed states expire after a config TTL.
    """

    __tablename__ = "armed_states"
    __table_args__ = (
        sa.UniqueConstraint("ticker", "cluster_id", name="uq_armed_ticker_cluster"),
        sa.CheckConstraint(
            "status IN ('armed', 'resolved', 'expired')", name="ck_armed_states_status"
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=lambda: uuid4().hex)
    ticker: Mapped[str] = mapped_column(sa.String(12), index=True)
    cluster_id: Mapped[str] = mapped_column(sa.ForeignKey("clusters.cluster_id"), index=True)
    catalyst_type: Mapped[str] = mapped_column(sa.String(32))
    event_ts: Mapped[datetime] = mapped_column(UTCDateTime)  # t0 = origin published_at
    armed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(sa.String(10), default="armed", index=True)
    resolution: Mapped[str | None] = mapped_column(sa.String(20))  # emitted|no_signal|ttl_no_bars
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SignalObservation(Base):
    """Signal-lab event-study row, one per ticker-attributed scored cluster
    (docs/ROADMAP.md task 5c.1). The lab grades INPUTS (do raw scores carry
    information?) on every scored cluster, not just threshold-crossers.

    t0 = origin published_at; entry price = first close strictly after t0 (I12).
    Backfilled/imported observations carry backfill=true and are excluded from
    headline lab stats by default (5c.4); their point-in-time fundamentals stay
    null — never faked from current values.
    """

    __tablename__ = "signal_observations"
    __table_args__ = (
        sa.CheckConstraint("status IN ('open', 'matured')", name="ck_signal_obs_status"),
    )

    observation_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(sa.ForeignKey("clusters.cluster_id"), index=True)
    ticker: Mapped[str] = mapped_column(sa.String(12), index=True)
    t0: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    entry_price_date: Mapped[date_ | None] = mapped_column(SafeDate)  # first close after t0
    features_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    marks_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)  # CAR at +1..+10d
    clean_window: Mapped[bool | None] = mapped_column()  # finalized at maturity (5c.3)
    novelty_rank: Mapped[int | None] = mapped_column(sa.Integer)
    backfill: Mapped[bool] = mapped_column(default=False, index=True)
    status: Mapped[str] = mapped_column(sa.String(8), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ScheduledEvent(Base):
    """A dated, forward-looking catalyst for the panel (docs/ROADMAP.md task 5b.1).

    Earnings dates (Finviz primary → yfinance fallback), M&A vote/close dates, PDUFA
    dates, and computed lockup expiries. status rolls upcoming → passed on the date.
    """

    __tablename__ = "scheduled_events"
    __table_args__ = (
        sa.UniqueConstraint(
            "ticker", "catalyst_type", "event_date", name="uq_scheduled_ticker_type_date"
        ),
        sa.CheckConstraint(
            "status IN ('upcoming', 'passed', 'cancelled')", name="ck_scheduled_status"
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=lambda: uuid4().hex)
    ticker: Mapped[str] = mapped_column(sa.String(12), index=True)
    catalyst_type: Mapped[str] = mapped_column(sa.String(32), index=True)
    event_date: Mapped[date_] = mapped_column(SafeDate, index=True)
    stage: Mapped[str | None] = mapped_column(sa.String(16))
    source: Mapped[str] = mapped_column(sa.String(32))  # provider/provenance
    status: Mapped[str] = mapped_column(sa.String(12), default="upcoming", index=True)
    meta_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RankingRun(Base):
    """Header for one ranker invocation (docs/ROADMAP.md task 7.2).

    The ranker only PROPOSES a cited watchlist — it never writes to configs or
    predictions (I6). Trigger is scheduled_daily / scheduled_weekly / manual; the
    manual force-run carries the operator-chosen model + timeframe. config_version
    is the context the ranker read against (traceability), not a config it created.
    """

    __tablename__ = "ranking_runs"
    __table_args__ = (
        sa.CheckConstraint(
            "trigger IN ('scheduled_daily', 'scheduled_weekly', 'manual')",
            name="ck_ranking_runs_trigger",
        ),
        sa.CheckConstraint("status IN ('ok', 'empty', 'failed')", name="ck_ranking_runs_status"),
    )

    run_id: Mapped[str] = mapped_column(
        sa.String(32), primary_key=True, default=lambda: uuid4().hex
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    trigger: Mapped[str] = mapped_column(sa.String(20), index=True)
    model: Mapped[str] = mapped_column(sa.String(48))
    horizon_trading_days: Mapped[int] = mapped_column(sa.Integer)  # operator "timeframe" swap
    filter_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    candidate_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    # Soft reference: the config context the ranker read against (traceability).
    # Not an FK — a ranking is a proposal artifact, never a ledger row (I6/I13).
    config_version: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    status: Mapped[str] = mapped_column(sa.String(8), default="ok")
    error: Mapped[str | None] = mapped_column(sa.Text)


class Ranking(Base):
    """One ranked candidate produced by a RankingRun (schema of task 7.2).

    {ticker, direction, conviction, rationale, evidence_ids} — evidence_ids point
    back at the cluster_ids in the evidence bundle, so every call is auditable.
    """

    __tablename__ = "rankings"
    __table_args__ = (
        sa.CheckConstraint(
            "direction IN ('bullish', 'bearish', 'neutral')", name="ck_rankings_direction"
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=lambda: uuid4().hex)
    run_id: Mapped[str] = mapped_column(sa.ForeignKey("ranking_runs.run_id"), index=True)
    rank: Mapped[int] = mapped_column(sa.Integer)
    ticker: Mapped[str] = mapped_column(sa.String(12), index=True)
    direction: Mapped[str] = mapped_column(sa.String(8))
    conviction: Mapped[float] = mapped_column(sa.Float)  # 0..1
    rationale: Mapped[str] = mapped_column(sa.Text)
    evidence_ids_json: Mapped[list[str]] = mapped_column(sa.JSON, default=list)


class LlmSpend(Base):
    """One row per model call (docs/ROADMAP.md task 7.2 — 'log token spend per call').

    Append-only cost ledger across every agent purpose (rank / analyst / deep_read).
    Cost is computed from token counts at the call site; the soft cap reads SUM(cost).
    """

    __tablename__ = "llm_spend"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=lambda: uuid4().hex)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    purpose: Mapped[str] = mapped_column(sa.String(20), index=True)  # rank|analyst|deep_read|smoke
    model: Mapped[str] = mapped_column(sa.String(48))
    run_id: Mapped[str | None] = mapped_column(sa.String(32), index=True)
    input_tokens: Mapped[int] = mapped_column(sa.Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(sa.Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(sa.Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(sa.Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(sa.Float, default=0.0)
    ok: Mapped[bool] = mapped_column(default=True)


class PendingChange(Base):
    """An analyst-proposed config change awaiting human approval (task 7.3, I3).

    The analyst writes a markdown report + a JSON patch here; it NEVER creates a
    config version itself. scripts/approve.py applies the patch to base params and
    calls get_or_create_config (the only path that mints a version). Rejection
    archives the row with a reason. resulting_config_version is set on approval.
    """

    __tablename__ = "pending_changes"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_pending_changes_status"
        ),
    )

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=lambda: uuid4().hex)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    base_config_version: Mapped[str] = mapped_column(sa.ForeignKey("configs.config_version"))
    patch_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)  # {path: value}
    rationale: Mapped[str] = mapped_column(sa.Text)
    report_md: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String(10), default="pending", index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_reason: Mapped[str | None] = mapped_column(sa.Text)
    resulting_config_version: Mapped[str | None] = mapped_column(
        sa.ForeignKey("configs.config_version")
    )


class AttentionDaily(Base):
    """Per-ticker daily attention rollup — news volume (structured + social) and
    mean sentiment — materialized from clusters + attributions.

    The substrate for buzz baselines and the ticker chart's attention series. It
    accumulates from BOTH the legacy import and the live pipeline (I13: derived,
    never a ledger row). Rebuilt idempotently by the attention rollup.
    """

    __tablename__ = "attention_daily"

    ticker: Mapped[str] = mapped_column(sa.String(12), primary_key=True)
    date: Mapped[date_] = mapped_column(SafeDate, primary_key=True)
    struct_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    social_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    sentiment_mean: Mapped[float | None] = mapped_column(sa.Float)  # mean finbert, that day
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class BuzzBaseline(Base):
    """Per-ticker social-volume baseline (mean/std of daily social_count),
    winsorized against meme spikes and shrunk toward the global mean.

    Warm-started from the legacy social slice and re-blended as fresh shadow data
    lands (source records which). buzz_z = (social_count - mean) / std. A ticker
    with too little social history has NO row -> no buzz (it stays price-only),
    which is exactly the tiering the design wants.
    """

    __tablename__ = "buzz_baselines"

    ticker: Mapped[str] = mapped_column(sa.String(12), primary_key=True)
    mean: Mapped[float] = mapped_column(sa.Float)
    std: Mapped[float] = mapped_column(sa.Float)
    n_days: Mapped[int] = mapped_column(sa.Integer)  # social-active days behind the estimate
    source: Mapped[str] = mapped_column(sa.String(24))  # warm_start | fresh | blend
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SearchInterestDaily(Base):
    """Per-ticker daily Google-Trends search interest — a NEW attention axis
    (experiment started 2026-07-20 so its baseline clock runs early).

    Google Trends returns each term's OWN 0-100 series (normalized to that term's
    trailing-window max), so ``interest`` is NOT cross-ticker comparable in
    absolute terms — the honest anomaly measure is a per-ticker z vs its OWN
    accumulated history (see search_interest_z, mirroring buzz_z). Descriptive /
    shadow only: NO signal path. Sourced from the unofficial endpoint (fail-soft),
    so coverage is best-effort. Each query returns the full trailing series, so a
    ticker's history is backfilled on first sight rather than accruing from zero.
    """

    __tablename__ = "search_interest_daily"

    ticker: Mapped[str] = mapped_column(sa.String(12), primary_key=True)
    date: Mapped[date_] = mapped_column(SafeDate, primary_key=True)
    interest: Mapped[float] = mapped_column(sa.Float)  # Google own-term relative 0-100
    term: Mapped[str] = mapped_column(sa.String(80))  # the query term, e.g. "AAPL stock"
    source: Mapped[str] = mapped_column(sa.String(24), default="google_trends")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TickerAnalysis(Base):
    """A single-ticker "deep dive" AI analysis (docs/ROADMAP.md task 7.4).

    An on-demand Claude read over ONE ticker's own-data evidence bundle (recent
    attributed clusters, both-axis scores, window composites, attention/buzz,
    ledger predictions, next earnings, fundamentals snapshot). Like a RankingRun
    it PROPOSES only (I6) and is a proposal artifact, never a ledger row (I13):
    config_version is a SOFT reference to the context read against, not an FK.

    Persisted so a revisit is instant; a re-run appends a fresh row (the latest by
    created_at is the one shown). Server-side rate limited to N distinct tickers
    per rolling window; every model call is logged to llm_spend (run_id=analysis_id).
    """

    __tablename__ = "ticker_analyses"
    __table_args__ = (
        sa.CheckConstraint("status IN ('ok', 'failed', 'empty')", name="ck_ticker_analyses_status"),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('bullish', 'bearish', 'neutral')",
            name="ck_ticker_analyses_direction",
        ),
    )

    analysis_id: Mapped[str] = mapped_column(
        sa.String(32), primary_key=True, default=lambda: uuid4().hex
    )
    ticker: Mapped[str] = mapped_column(sa.String(12), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    model: Mapped[str] = mapped_column(sa.String(48))
    horizon_trading_days: Mapped[int] = mapped_column(sa.Integer)
    # Soft reference to the config context read against (traceability), not an FK
    # — a deep dive is a proposal artifact, never a ledger row (I6/I13).
    config_version: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    status: Mapped[str] = mapped_column(sa.String(8), default="ok")
    direction: Mapped[str | None] = mapped_column(sa.String(8))
    conviction: Mapped[float | None] = mapped_column(sa.Float)
    thesis: Mapped[str | None] = mapped_column(sa.Text)
    key_evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, default=list)
    risks_json: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    what_would_change_json: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    # The assembled own-data evidence snapshot the call read (auditability).
    evidence_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    error: Mapped[str | None] = mapped_column(sa.Text)


class PredictionContext(Base):
    """Companion origin-news context for a prediction (the LEDGER lane feature).

    ``predictions`` is append-only after issue except the grader fields (I4), so a
    prediction's originating-news provenance — source_class (the STRUCTURED vs SOCIAL
    lane), headline, url, source — is carried HERE, keyed by prediction_id, instead
    of on the immutable ledger row. Freely mutable/backfillable: this is a derived
    convenience table, NOT a ledger row.

    Written at arm time from the live cluster/raw_items join (see
    pipeline.common.prediction_context) and backfilled for history from the full
    local DB during seed export. ``cluster_id`` is a SOFT pointer (not an FK): the
    cluster family is deliberately NOT shipped in the slim seed, so an FK would
    dangle on the hydrated Railway volume.
    """

    __tablename__ = "prediction_context"
    __table_args__ = (
        sa.CheckConstraint(
            "source_class IS NULL OR source_class IN ('structured', 'social', 'mixed')",
            name="ck_prediction_context_source_class",
        ),
    )

    prediction_id: Mapped[str] = mapped_column(
        sa.ForeignKey("predictions.prediction_id"), primary_key=True
    )
    # The lane the LEDGER splits on. null when the originating cluster could not be
    # resolved (kept honest — never guessed). 'mixed' if contributing origins disagree.
    source_class: Mapped[str | None] = mapped_column(sa.String(12), index=True)
    headline: Mapped[str | None] = mapped_column(sa.Text)
    url: Mapped[str | None] = mapped_column(sa.Text)
    source: Mapped[str | None] = mapped_column(sa.String(200))
    cluster_id: Mapped[str | None] = mapped_column(sa.String(64))  # resolved origin (soft ref)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


# --- I2: raw_items append-only ------------------------------------------------


@event.listens_for(RawItem, "before_update")
def _raw_items_no_update(mapper: Any, connection: Any, target: RawItem) -> None:
    raise AppendOnlyViolation("raw_items is append-only: UPDATE forbidden (I2)")


@event.listens_for(RawItem, "before_delete")
def _raw_items_no_delete(mapper: Any, connection: Any, target: RawItem) -> None:
    raise AppendOnlyViolation("raw_items is append-only: DELETE forbidden (I2)")


# DB-level triggers catch raw SQL that bypasses the ORM.
# ROADMAP-NOTE: SQLite trigger syntax; the Postgres cutover needs equivalent
# CREATE TRIGGER ... EXECUTE FUNCTION guards (ORM hooks still apply meanwhile).
for _ddl in (
    "CREATE TRIGGER raw_items_no_update BEFORE UPDATE ON raw_items "
    "BEGIN SELECT RAISE(ABORT, 'raw_items is append-only (I2)'); END",
    "CREATE TRIGGER raw_items_no_delete BEFORE DELETE ON raw_items "
    "BEGIN SELECT RAISE(ABORT, 'raw_items is append-only (I2)'); END",
):
    event.listen(RawItem.__table__, "after_create", sa.DDL(_ddl).execute_if(dialect="sqlite"))


# --- I3: configs immutable once created ---------------------------------------


@event.listens_for(Config, "before_update")
def _configs_immutable(mapper: Any, connection: Any, target: Config) -> None:
    raise ImmutableRowViolation(
        "configs rows are immutable; create a new config_version instead (I3)"
    )


@event.listens_for(Config, "before_delete")
def _configs_no_delete(mapper: Any, connection: Any, target: Config) -> None:
    raise ImmutableRowViolation("configs rows cannot be deleted (I3)")


# --- I4: predictions editable only by the grader outcome fields ----------------

_GRADER_FIELDS = {"status", "outcome", "realized_adjusted_return", "graded_at", "resolving_close"}


@event.listens_for(Prediction, "before_update")
def _predictions_grader_only(mapper: Any, connection: Any, target: Prediction) -> None:
    changed = {attr.key for attr in sa.inspect(target).attrs if attr.history.has_changes()}
    illegal = changed - _GRADER_FIELDS
    if illegal:
        raise ImmutableRowViolation(
            f"predictions rows are immutable after issue except grader fields (I4); "
            f"attempted to change: {sorted(illegal)}"
        )


@event.listens_for(Prediction, "before_delete")
def _predictions_no_delete(mapper: Any, connection: Any, target: Prediction) -> None:
    raise ImmutableRowViolation("predictions rows cannot be deleted (I4)")


class SimConfig(Base):
    """A frozen paper-trading strategy configuration (Phase 2 racing rails).

    Racing discipline: params_json is FROZEN at creation (walk-forward rule —
    a config is judged only on data after its freeze; tuning means a NEW
    config). `enabled` is the per-config paper switch; the master switch is the
    SIM_ENABLED env. gate_ref records which docs/gates.md stage licensed it —
    rails ship with zero configs until a gate passes for an equity expression.
    """

    __tablename__ = "sim_configs"

    config_id: Mapped[str] = mapped_column(
        sa.String(32), primary_key=True, default=lambda: uuid4().hex
    )
    name: Mapped[str] = mapped_column(sa.String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    params_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    enabled: Mapped[bool] = mapped_column(default=False)
    gate_ref: Mapped[str | None] = mapped_column(sa.String(32))
    notes: Mapped[str | None] = mapped_column(sa.Text)


class SimTrade(Base):
    """One immutable paper trade (Phase 2). Entry rows are append-only; the only
    legal mutation is the single open -> closed transition filling exit fields.
    features_json snapshots decision-time state (lookahead hygiene)."""

    __tablename__ = "sim_trades"
    __table_args__ = (
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_sim_trades_status"),
        sa.CheckConstraint("direction IN (1, -1)", name="ck_sim_trades_direction"),
    )

    trade_id: Mapped[str] = mapped_column(
        sa.String(32), primary_key=True, default=lambda: uuid4().hex
    )
    config_id: Mapped[str] = mapped_column(sa.ForeignKey("sim_configs.config_id"), index=True)
    ticker: Mapped[str] = mapped_column(sa.String(12), index=True)
    direction: Mapped[int] = mapped_column(sa.Integer)
    entered_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    entry_price: Mapped[float] = mapped_column(sa.Float)
    entry_source: Mapped[str] = mapped_column(sa.String(20))  # alpaca-iex | daily-close
    horizon_trading_days: Mapped[int] = mapped_column(sa.Integer)
    features_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    cluster_id: Mapped[str | None] = mapped_column(sa.String(64))
    status: Mapped[str] = mapped_column(sa.String(8), default="open", index=True)
    exited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    exit_price: Mapped[float | None] = mapped_column(sa.Float)
    exit_reason: Mapped[str | None] = mapped_column(sa.String(16))  # horizon | close | manual
    gross_return: Mapped[float | None] = mapped_column(sa.Float)
    net_return: Mapped[float | None] = mapped_column(sa.Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Broker execution provenance (paper): which broker filled this, and the real
    # Alpaca order ids for the entry and exit legs. Null when the trade was priced
    # from a quote source rather than a live paper order (tests / quote-only runs).
    # The entry id is set at creation; the exit id fills on the open->closed leg.
    broker: Mapped[str | None] = mapped_column(sa.String(20))
    broker_entry_order_id: Mapped[str | None] = mapped_column(sa.String(48))
    broker_exit_order_id: Mapped[str | None] = mapped_column(sa.String(48))


_SIM_EXIT_FIELDS = {
    "status",
    "exited_at",
    "exit_price",
    "exit_reason",
    "gross_return",
    "net_return",
    "broker_exit_order_id",  # the exit-leg order id fills on the open->closed transition
}


@event.listens_for(SimTrade, "before_update")
def _sim_trades_immutable(mapper: Any, connection: Any, target: SimTrade) -> None:
    changed = {attr.key for attr in sa.inspect(target).attrs if attr.history.has_changes()}
    status_hist = sa.inspect(target).attrs.status.history
    was_closed = bool(status_hist.deleted and status_hist.deleted[0] == "closed") or (
        not status_hist.has_changes() and target.status == "closed" and changed
    )
    illegal = changed - _SIM_EXIT_FIELDS
    if was_closed or illegal:
        raise ImmutableRowViolation(
            "sim_trades rows are immutable except the single open->closed exit "
            f"transition; attempted to change: {sorted(changed)}"
        )


@event.listens_for(SimTrade, "before_delete")
def _sim_trades_no_delete(mapper: Any, connection: Any, target: SimTrade) -> None:
    raise ImmutableRowViolation("sim_trades rows cannot be deleted")


class SimDailySummary(Base):
    """One EOD report-card row per (trading day, config) — a durable rollup of the
    day's paper session so daily report cards are a cheap read, not a re-scan of
    the whole immutable ledger. Unlike sim_trades this is a MUTABLE rollup: it is
    recomputed/upserted at each EOD flatten (and can be re-derived from sim_trades
    at any time), so it carries no immutability hook. P&L is honest-costs net
    (COST_RT already applied in sim_trades.net_return); pnl_dollars = sum(net *
    per-trade notional)."""

    __tablename__ = "sim_daily_summary"

    session_date: Mapped[date_] = mapped_column(SafeDate, primary_key=True)
    config_id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    config_name: Mapped[str] = mapped_column(sa.String(64))
    trades: Mapped[int] = mapped_column(sa.Integer, default=0)  # closed (realized) this day
    open_eod: Mapped[int] = mapped_column(sa.Integer, default=0)  # still-open at summary time
    wins: Mapped[int] = mapped_column(sa.Integer, default=0)
    losses: Mapped[int] = mapped_column(sa.Integer, default=0)
    hit_rate: Mapped[float | None] = mapped_column(sa.Float)  # wins/closed, null if 0 closed
    mean_net: Mapped[float | None] = mapped_column(sa.Float)  # fractional net/trade
    sum_net: Mapped[float | None] = mapped_column(sa.Float)  # fractional net summed
    pnl_dollars: Mapped[float | None] = mapped_column(sa.Float)  # sum(net * notional)
    spy_ref: Mapped[float | None] = mapped_column(sa.Float)  # SPY last at EOD (tape ref)
    gate_ref: Mapped[str | None] = mapped_column(sa.String(48))  # provenance (exploratory vs gated)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PremarketPanel(Base):
    """One frozen premarket catalyst panel per trading day (PMR feature).

    The morning ranking is a point-in-time artifact (I12): rows_json freezes at
    first compute >= 08:30 ET and is never rewritten — the ONLY post-freeze
    mutation is the post-close report card (graded_at / outcomes_json /
    summary_json), enforced by the ORM guard below. That immutability is what
    makes accumulated panels usable as evidence for a future gated sim config.
    """

    __tablename__ = "premarket_panels"

    session_date: Mapped[date_] = mapped_column(SafeDate, primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime)
    rows_json: Mapped[list] = mapped_column(sa.JSON)  # ranked PremarketRow dicts
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Report card (deliverable 9) — the sole mutable surface:
    graded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    outcomes_json: Mapped[dict | None] = mapped_column(sa.JSON)  # ticker -> gap/oc/lean_hit
    summary_json: Mapped[dict | None] = mapped_column(sa.JSON)  # top5-vs-rest, hit rate


class ExtendedSessionDaily(Base):
    """Per-(ticker, session_date) extended-hours + regular-session price record.

    The substrate for the PREMARKET/EXTENDED tracker: one row per trading day per
    tracked ticker, so pre/regular/post behavior accumulates and days are
    comparable. A MUTABLE two-phase rollup (like sim_daily_summary, no immutability
    hook): the premarket phase (logged shortly after 09:30 ET) fills prior_close /
    reg_open / the pm_* extended-hours fields; the postmarket phase (after the
    extended close, or a next-day catch-up) fills reg_close / reg_pct and the ah_*
    fields. Regular-session prices come from the daily bar cache (robust, fully
    backfillable); pm_*/ah_* come from intraday prepost bars (best-effort — thin
    names simply have no extended prints, stored NULL and shown as '--', never
    fabricated). All % are fractions (0.03 = 3%)."""

    __tablename__ = "extended_session_daily"
    __table_args__ = (
        # movers query is WHERE session_date = ? (all tickers on a day); the
        # composite PK leads with ticker, so date needs its own index.
        sa.Index("ix_extended_session_daily_date", "session_date"),
    )

    ticker: Mapped[str] = mapped_column(sa.String(12), primary_key=True)
    session_date: Mapped[date_] = mapped_column(SafeDate, primary_key=True)
    prior_close: Mapped[float | None] = mapped_column(sa.Float)  # prior trading day's close
    # Premarket (extended, best-effort): last print + move vs prior close.
    pm_last: Mapped[float | None] = mapped_column(sa.Float)
    pm_pct: Mapped[float | None] = mapped_column(sa.Float)  # pm_last / prior_close - 1
    pm_high: Mapped[float | None] = mapped_column(sa.Float)
    pm_low: Mapped[float | None] = mapped_column(sa.Float)
    pm_volume: Mapped[int | None] = mapped_column(sa.Integer)
    # Regular session (from the daily cache — robust): open, close, day change.
    reg_open: Mapped[float | None] = mapped_column(sa.Float)
    reg_close: Mapped[float | None] = mapped_column(sa.Float)
    reg_pct: Mapped[float | None] = mapped_column(sa.Float)  # reg_close / prior_close - 1
    # Afterhours (extended, best-effort, same-day only): last print + move vs close.
    ah_last: Mapped[float | None] = mapped_column(sa.Float)
    ah_pct: Mapped[float | None] = mapped_column(sa.Float)  # ah_last / reg_close - 1
    ah_volume: Mapped[int | None] = mapped_column(sa.Integer)
    source: Mapped[str] = mapped_column(sa.String(16), default="yfinance")  # provenance
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class WatchlistPin(Base):
    """A user-pinned ticker for the TRADER watchlist lane (Phase 3).

    Deliberately stored in OUR DB (not Alpaca's watchlist store) so each pin wires
    into the local catalyst / buzz / armed-state machinery — the lane shows an
    'armed — waiting for catalyst' read per pin, which Alpaca's flat symbol list
    couldn't express. This is a MUTABLE user table (pin/unpin), carries no
    immutability hook, and never places orders — view/stage only.
    """

    __tablename__ = "watchlist_pins"

    ticker: Mapped[str] = mapped_column(sa.String(12), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    note: Mapped[str | None] = mapped_column(sa.Text)
    source: Mapped[str] = mapped_column(sa.String(16), default="user")  # provenance


# --- PMR: panels frozen except the report-card fields ---------------------------

_PREMARKET_GRADE_FIELDS = {"graded_at", "outcomes_json", "summary_json"}


@event.listens_for(PremarketPanel, "before_update")
def _premarket_grade_only(mapper: Any, connection: Any, target: PremarketPanel) -> None:
    changed = {attr.key for attr in sa.inspect(target).attrs if attr.history.has_changes()}
    illegal = changed - _PREMARKET_GRADE_FIELDS
    if illegal:
        raise ImmutableRowViolation(
            "premarket_panels rows are frozen after snapshot except report-card "
            f"fields; attempted to change: {sorted(illegal)}"
        )


@event.listens_for(PremarketPanel, "before_delete")
def _premarket_no_delete(mapper: Any, connection: Any, target: PremarketPanel) -> None:
    raise ImmutableRowViolation("premarket_panels rows cannot be deleted")
