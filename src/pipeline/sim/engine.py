"""Paper-sim engine (Phase 2 rails) — config racing over live scored clusters.

Hypothesis-agnostic infrastructure: configs are FROZEN filter+direction+horizon
specs (racing discipline — tuning means a new config), trades are immutable
rows with decision-time feature snapshots, and everything is paper-only. The
rails ship with ZERO configs: per docs/gates.md, a config is only seeded once
its hypothesis passes a pre-registered gate. Master switch = SIM_ENABLED env
(default off) on top of each config's own `enabled` flag.

Entry: a fresh scored cluster matching the config's filter terms -> one open
trade per (config, ticker) with a 24h re-entry cooldown, entry price from the
injected quote source (Alpaca latest trade in production; fake in tests) —
no quote, no trade (never fabricate a fill).
Exit: at the config's horizon (trading days approximated via UTC weekdays),
priced from the same quote source; net = gross - COST_RT. Honest costs only.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.common.events import publish_event
from pipeline.common.models import (
    Cluster,
    ClusterEntity,
    ClusterScore,
    FundamentalsSnapshot,
    RawItem,
    SimConfig,
    SimTrade,
)
from pipeline.common.timeutil import utcnow
from pipeline.sim.exitpolicy import decide_exit, resolve_exit_policy

log = logging.getLogger("pipeline.sim")

COST_RT = 0.0050  # accepted flat round-trip cost model
_ENTRY_LOOKBACK_MIN = 30  # fresh clusters = scored for items published in this window
_REENTRY_COOLDOWN_H = 24
# Intraday (horizon_trading_days == 0) exit cutoff: 15:50 ET, i.e. 19:50 UTC
# under EDT — before the 16:00 close so a paper market order still fills in RTH.
# DST caveat (documented): this is EDT-correct; in EST it maps to 14:50 ET, still
# comfortably intraday. A one-day mockup in July runs under EDT.
_INTRADAY_EXIT_UTC = (19, 50)

QuoteFn = Callable[[str], float | None]


def sim_enabled() -> bool:
    """Master paper-sim switch (env). Default OFF — the on/off the plan requires."""
    return (os.environ.get("SIM_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


# --- Entry loss guards (2026-07-28) ----------------------------------------
# ENTRY-ONLY circuit breakers: a config (or the whole book) that has bled enough
# REALIZED loss in the current session stops OPENING new positions for the rest
# of that session. Exits and the EOD flatten are NEVER gated by these (a stop
# that can't close is worse than useless — see evaluate_exits/is_close). Realized
# only: unrealized/open P&L is intentionally out of scope here (intraday configs
# realize at the 15:50 flatten, so the cap mainly gates multi-day bleed and the
# late session — an exposure-based variant is a documented future option).
_DEFAULT_NOTIONAL = 1000.0  # $ fallback when a trade row lacks a snapshotted notional
_ET_UTC_OFFSET_H = 4  # EDT approximation (matches _INTRADAY_EXIT_UTC's 15:50 ET = 19:50 UTC)


def _loss_cap_env(name: str, default: float) -> float:
    """A loss cap in POSITIVE $ (the guard fires when realized P&L <= -cap). Env
    override; a value <= 0 disables that guard entirely (documented off-switch)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid %s=%r — using default $%.0f", name, raw, default)
        return default


def config_loss_cap_usd() -> float:
    return _loss_cap_env("SIM_CONFIG_LOSS_CAP_USD", 150.0)


def portfolio_loss_cap_usd() -> float:
    return _loss_cap_env("SIM_PORTFOLIO_LOSS_CAP_USD", 400.0)


def _default_day_start_utc(now: datetime) -> datetime:
    """UTC instant of the current ET calendar-day start (EDT approximation,
    consistent with the engine's other ET-as-UTC constants). Scopes 'realized
    day P&L' to today's session without threading a tz through every caller."""
    et_date = (now - timedelta(hours=_ET_UTC_OFFSET_H)).date()
    return datetime(
        et_date.year, et_date.month, et_date.day, _ET_UTC_OFFSET_H, 0, 0, tzinfo=now.tzinfo
    )


def _realized_day_pnl(
    session: Session, day_start_utc: datetime, now: datetime
) -> tuple[dict[str, float], float]:
    """(per-config $ realized P&L, total $) over trades CLOSED in [day_start, now].
    Same honest-costs $ weighting as sim/daily.write_daily_summary (net_return is
    already net of COST_RT; weight by the trade's snapshotted notional)."""
    rows = session.execute(
        select(SimTrade.config_id, SimTrade.net_return, SimTrade.features_json).where(
            SimTrade.status == "closed",
            SimTrade.exited_at >= day_start_utc,
            SimTrade.exited_at <= now,
        )
    ).all()
    per: dict[str, float] = {}
    total = 0.0
    for cid, net, feat in rows:
        if net is None:
            continue
        notional = float((feat or {}).get("notional") or _DEFAULT_NOTIONAL)
        pnl = net * notional
        per[cid] = per.get(cid, 0.0) + pnl
        total += pnl
    return per, total


def entry_guard_status(
    session: Session, *, now: datetime | None = None, day_start_utc: datetime | None = None
) -> dict[str, Any]:
    """Current entry-guard state for /sim/status: the caps, today's realized $ per
    the same scope evaluate_entries uses, and which configs / the whole book are
    currently halted from OPENING new positions. Read-only."""
    now = now or utcnow()
    day_start = day_start_utc or _default_day_start_utc(now)
    cfg_cap = config_loss_cap_usd()
    port_cap = portfolio_loss_cap_usd()
    per, total = _realized_day_pnl(session, day_start, now)
    id_to_name = {c.config_id: c.name for c in session.execute(select(SimConfig)).scalars()}
    halted = sorted(
        id_to_name.get(cid, cid)
        for cid, p in per.items()
        if cfg_cap > 0 and p <= -cfg_cap
    )
    return {
        "config_loss_cap_usd": cfg_cap,
        "portfolio_loss_cap_usd": port_cap,
        "day_start": day_start.isoformat(),
        "day_realized_usd": round(total, 2),
        "portfolio_halted": bool(port_cap > 0 and total <= -port_cap),
        "halted_configs": halted,
    }


def _cluster_matches(params: dict[str, Any], feat: dict[str, Any]) -> bool:
    """Config filter terms over a scored cluster's features (preset vocabulary)."""
    if params.get("catalyst_types") and feat.get("catalyst_type") not in params["catalyst_types"]:
        return False
    if params.get("high_alert_only") and not feat.get("high_alert"):
        return False
    min_mat = params.get("min_materiality")
    if min_mat and (feat.get("materiality") or 0.0) < min_mat:
        return False
    if params.get("min_abs_sentiment"):
        fb = feat.get("finbert_score")
        if fb is None or abs(fb) < params["min_abs_sentiment"]:
            return False
    # max_mcap_musd: cap ceiling in $ MILLIONS (fundamentals_snapshots units).
    # Fail-closed on unknown cap — a small-cap filter must not trade unknowns.
    max_mcap = params.get("max_mcap_musd")
    if max_mcap is not None:
        mc = feat.get("market_cap_musd")
        if mc is None or mc > max_mcap:
            return False
    return not (params.get("after_hours_only") and not feat.get("after_hours"))


# Structural directional prior by catalyst type/role. Only UNAMBIGUOUS cases get
# a sign; anything else returns None (no trade — never coin-flip). This is the
# "typed-direction" hypothesis: does the catalyst TYPE alone predict direction?
#   - secondary_offering / dilution: new shares are structurally dilutive -> short
#   - M&A target: acquired at a premium -> long; acquirer is ambiguous -> skip
def _typed_direction(feat: dict[str, Any]) -> int | None:
    ct = (feat.get("catalyst_type") or "").lower()
    role = (feat.get("ticker_role") or "").lower()
    if ct in ("secondary_offering", "dilution", "atm_offering"):
        return -1
    if ct in ("ma", "merger", "acquisition") and role == "target":
        return 1
    return None


def _direction(params: dict[str, Any], feat: dict[str, Any]) -> int | None:
    """Direction per the config spec. None = no valid direction -> no trade
    (the SDOT lesson: never coin-flip a neutral signal)."""
    src = params.get("direction", "finbert_sign")
    if src == "long":
        return 1
    if src == "finbert_sign":
        fb = feat.get("finbert_score")
        cutoff = params.get("direction_min_abs", 0.3)
        if fb is None or abs(fb) < cutoff:
            return None
        return 1 if fb > 0 else -1
    if src == "catalyst_typed":
        return _typed_direction(feat)
    return None


def _mcap_by_ticker(session: Session, tickers: set[str]) -> dict[str, float | None]:
    """Latest-snapshot market cap ($ MILLIONS) per ticker, one batched query.
    Decision-time feature: the newest fundamentals_snapshots row per ticker."""
    if not tickers:
        return {}
    latest = (
        select(FundamentalsSnapshot.ticker, func.max(FundamentalsSnapshot.as_of).label("as_of"))
        .where(FundamentalsSnapshot.ticker.in_(tickers))
        .group_by(FundamentalsSnapshot.ticker)
        .subquery()
    )
    rows = session.execute(
        select(FundamentalsSnapshot.ticker, FundamentalsSnapshot.market_cap).join(
            latest,
            (FundamentalsSnapshot.ticker == latest.c.ticker)
            & (FundamentalsSnapshot.as_of == latest.c.as_of),
        )
    ).all()
    return {t: mc for t, mc in rows}


def _fresh_scored_clusters(session: Session, since: datetime) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ClusterScore, Cluster, RawItem, ClusterEntity.ticker, ClusterEntity.ticker_role)
        .join(Cluster, Cluster.cluster_id == ClusterScore.cluster_id)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .join(ClusterEntity, ClusterEntity.cluster_id == Cluster.cluster_id)
        .where(RawItem.published_at >= since)
        .where(RawItem.source_class == "structured")
    ).all()
    out = []
    for cs, cl, origin, ticker, ticker_role in rows:
        hour = origin.published_at.hour + origin.published_at.minute / 60.0
        after_hours = not (13.5 <= hour < 20.0)  # UTC approx of 9:30-16:00 ET
        out.append(
            {
                "cluster_id": cl.cluster_id,
                "ticker": ticker,
                "ticker_role": ticker_role,
                "published_at": origin.published_at.isoformat(),  # JSON-safe snapshot
                "catalyst_type": cs.catalyst_type,
                "event_stage": cs.event_stage,
                "finbert_score": cs.finbert_score,
                "lm_score": cs.lm_score,
                "materiality": cs.materiality,
                "high_alert": bool(cs.high_alert),
                "after_hours": after_hours,
            }
        )
    # Attach decision-time market cap ($M) so configs can filter on size and the
    # value is snapshotted into features_json with everything else.
    mcaps = _mcap_by_ticker(session, {r["ticker"] for r in out})
    for r in out:
        r["market_cap_musd"] = mcaps.get(r["ticker"])
    return out


def evaluate_entries(
    session: Session,
    quote: QuoteFn,
    *,
    now: datetime | None = None,
    broker: Any = None,
    day_start_utc: datetime | None = None,
) -> list[SimTrade]:
    """Open trades for enabled configs against fresh scored clusters.

    ``broker`` (an AlpacaPaperBroker or None): when present, entries are REAL
    paper orders — the quote sizes the order, Alpaca fills it, and the reconciled
    fill (never the quote) becomes entry_price; a non-fill writes no trade. When
    None, entry_price is the quote (the original quote-only path, used by tests).

    ENTRY LOSS GUARDS: before opening anything, the portfolio kill switch (total
    realized day loss <= -SIM_PORTFOLIO_LOSS_CAP_USD) blocks ALL new entries; and
    per config, a realized day loss <= -SIM_CONFIG_LOSS_CAP_USD halts that config's
    entries for the rest of the session. Exits are unaffected. ``day_start_utc``
    scopes 'today' (defaults to the current ET calendar day)."""
    now = now or utcnow()
    day_start = day_start_utc or _default_day_start_utc(now)
    cfg_cap = config_loss_cap_usd()
    port_cap = portfolio_loss_cap_usd()
    per_pnl, total_pnl = _realized_day_pnl(session, day_start, now)

    # (b) Portfolio daily kill switch — one loud line, then no entries this sweep.
    if port_cap > 0 and total_pnl <= -port_cap:
        log.warning(
            "PORTFOLIO KILL SWITCH ACTIVE: day realized $%.2f <= -$%.0f — no new entries "
            "for any config this session (exits/flatten unaffected)",
            total_pnl, port_cap,
        )
        return []

    configs = (
        session.execute(select(SimConfig).where(SimConfig.enabled.is_(True))).scalars().all()
    )
    if not configs:
        return []
    fresh = _fresh_scored_clusters(session, now - timedelta(minutes=_ENTRY_LOOKBACK_MIN))
    opened: list[SimTrade] = []
    capped: list[str] = []
    for cfg in configs:
        # (a) Per-config daily loss cap — halt this config's entries this session.
        if cfg_cap > 0 and per_pnl.get(cfg.config_id, 0.0) <= -cfg_cap:
            capped.append(cfg.name)
            continue
        p = cfg.params_json or {}
        for feat in fresh:
            if not _cluster_matches(p, feat):
                continue
            dirn = _direction(p, feat)
            if dirn is None:
                continue
            ticker = feat["ticker"]
            # one open position per (config, ticker); 24h re-entry cooldown
            recent = session.execute(
                select(SimTrade)
                .where(SimTrade.config_id == cfg.config_id)
                .where(SimTrade.ticker == ticker)
                .order_by(SimTrade.entered_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if recent is not None and (
                recent.status == "open"
                or (now - recent.entered_at) < timedelta(hours=_REENTRY_COOLDOWN_H)
            ):
                continue
            px = quote(ticker)
            if px is None or px <= 0:
                continue  # no quote -> no trade; fills are never fabricated

            trade = _open_trade(cfg, feat, dirn, float(px), now, broker)
            if trade is None:
                continue  # broker: order not filled -> no position (no-fill == no-trade)
            session.add(trade)
            opened.append(trade)
    if capped:
        log.warning(
            "ENTRY CAP active this sweep — halting new entries for %s "
            "(per-config day realized loss <= -$%.0f; exits unaffected)",
            sorted(capped), cfg_cap,
        )
    if opened:
        session.commit()
        publish_event("sim_trades", opened=len(opened))
    return opened


def _open_trade(
    cfg: SimConfig, feat: dict[str, Any], dirn: int, px: float, now: datetime, broker: Any
) -> SimTrade | None:
    """Build one SimTrade. With a broker, place a real paper order and use the
    reconciled fill as entry_price (None if it doesn't fill)."""
    p = cfg.params_json or {}
    horizon = int(p.get("horizon_trading_days", 3))
    entry_price = px
    entry_source = p.get("entry_source", "alpaca-iex")
    broker_name: str | None = None
    entry_order_id: str | None = None
    qty: int | None = None

    if broker is not None:
        if broker.open_position_count() >= broker.max_open:
            log.info("max open positions reached — skipping %s", feat["ticker"])
            return None
        qty = int(broker.notional // px)  # whole shares within the fixed notional
        if qty < 1:
            log.info("notional $%.0f < 1 share of %s @ $%.2f — skip", broker.notional, feat["ticker"], px)
            return None
        try:
            entry_order_id = broker.submit_market(feat["ticker"], dirn, qty)
            fill = broker.reconcile(entry_order_id)
        except Exception as exc:  # noqa: BLE001 — a broker error must not crash the sweep
            log.warning("entry order failed for %s: %s", feat["ticker"], exc)
            return None
        if fill is None or fill.filled_avg_price is None:
            return None  # no fill -> no position
        entry_price = float(fill.filled_avg_price)
        entry_source = "alpaca-paper"
        broker_name = "alpaca-paper"
        qty = int(fill.filled_qty or qty)

    return SimTrade(
        config_id=cfg.config_id,
        ticker=feat["ticker"],
        direction=dirn,
        entered_at=now,
        entry_price=entry_price,
        entry_source=entry_source,
        horizon_trading_days=horizon,
        features_json=feat | {"config_params": p, "qty": qty, "notional": (broker.notional if broker else None)},
        cluster_id=feat["cluster_id"],
        created_at=now,
        broker=broker_name,
        broker_entry_order_id=entry_order_id,
    )


def _trading_days_elapsed(start: datetime, end: datetime) -> int:
    """UTC weekday count between two datetimes (holiday-blind approximation,
    documented; exact trading calendars arrive with the marking integration)."""
    days = 0
    d = start.date()
    while d < end.date():
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _intraday_should_exit(entered_at: datetime, now: datetime) -> bool:
    """Intraday (horizon 0) exit: at/after today's 15:50-ET cutoff, or once the
    entry day has rolled over (defensive — a weekend gap should never strand a
    position). Keeps the mockup's 'exits before close' guarantee."""
    cutoff = now.replace(
        hour=_INTRADAY_EXIT_UTC[0], minute=_INTRADAY_EXIT_UTC[1], second=0, microsecond=0
    )
    return now >= cutoff or now.date() > entered_at.date()


def _horizon_reached(t: SimTrade, now: datetime) -> tuple[bool, str]:
    """The baseline horizon / intraday-cutoff gate — also the MAX-HOLD backstop
    every exit policy inherits. Reasons preserved verbatim from pre-policy behavior."""
    if t.horizon_trading_days == 0:
        return _intraday_should_exit(t.entered_at, now), "close"
    return (_trading_days_elapsed(t.entered_at, now) >= t.horizon_trading_days), "horizon"


def _should_exit(
    t: SimTrade, now: datetime, *, force: bool, quote: QuoteFn | None = None
) -> tuple[bool, str]:
    """(exit?, reason). ``force`` closes everything (EOD flatten) — NEVER gated by
    a policy. Otherwise the trade's FROZEN ``exit_policy`` (snapshotted in
    features_json at entry) decides, backstopped by the horizon. A trade with no
    exit_policy is horizon-hold = the exact pre-policy behavior, and does not even
    fetch a quote here (default path unchanged)."""
    if force:
        return True, "close"
    feat = t.features_json or {}
    policy = resolve_exit_policy(feat.get("config_params") or {}, feat.get("catalyst_type"))
    horizon_reached, horizon_reason = _horizon_reached(t, now)
    if policy.get("kind", "horizon_hold") == "horizon_hold":
        return horizon_reached, horizon_reason  # byte-identical to the original behavior
    price = quote(t.ticker) if quote is not None else None
    if price is None or price <= 0:
        return horizon_reached, horizon_reason  # can't price the policy -> horizon backstop
    d = decide_exit(
        policy,
        {
            "entry": t.entry_price,
            "price": float(price),
            "direction": t.direction,
            "horizon_reached": horizon_reached,
            "horizon_reason": horizon_reason,
            "held_hours": (now - t.entered_at).total_seconds() / 3600.0,
        },
    )
    return d.exit, d.reason


def evaluate_exits(
    session: Session,
    quote: QuoteFn,
    *,
    now: datetime | None = None,
    broker: Any = None,
    force: bool = False,
) -> list[SimTrade]:
    """Close open trades whose horizon has elapsed (or all of them when force),
    reconciling the exit fill from Alpaca when a broker is present."""
    now = now or utcnow()
    open_trades = (
        session.execute(select(SimTrade).where(SimTrade.status == "open")).scalars().all()
    )
    closed: list[SimTrade] = []
    for t in open_trades:
        do_exit, reason = _should_exit(t, now, force=force, quote=quote)
        if not do_exit:
            continue

        exit_price: float | None = None
        exit_order_id: str | None = None
        if broker is not None and t.broker == "alpaca-paper":
            qty = int((t.features_json or {}).get("qty") or 0)
            if qty >= 1:
                try:
                    # Close = opposite side of the entry direction. is_close: a
                    # flatten must never be blocked by the entry cap.
                    exit_order_id = broker.submit_market(t.ticker, -t.direction, qty, is_close=True)
                    fill = broker.reconcile(exit_order_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("exit order failed for %s: %s", t.ticker, exc)
                    fill = None
                if fill is None or fill.filled_avg_price is None:
                    continue  # not filled -> leave open, retry next sweep
                exit_price = float(fill.filled_avg_price)
        if exit_price is None:
            px = quote(t.ticker)
            if px is None or px <= 0:
                continue  # try again next sweep
            exit_price = float(px)

        gross = t.direction * (exit_price / t.entry_price - 1.0)
        t.status = "closed"
        t.exited_at = now
        t.exit_price = exit_price
        t.exit_reason = reason
        t.gross_return = round(gross, 6)
        t.net_return = round(gross - COST_RT, 6)
        if exit_order_id:
            t.broker_exit_order_id = exit_order_id
        closed.append(t)
    if closed:
        session.commit()
        publish_event("sim_trades", closed=len(closed))
    return closed


def run_sim_cycle(
    session: Session,
    quote: QuoteFn,
    *,
    now: datetime | None = None,
    broker: Any = None,
    force_exit: bool = False,
    day_start_utc: datetime | None = None,
) -> str:
    """One sim pass (called from the pipeline's fast sweep when SIM_ENABLED, or
    from the standalone mockup driver). ``force_exit`` flattens everything (EOD).
    ``day_start_utc`` scopes the entry loss guards to the session (default: ET day)."""
    opened = (
        []
        if force_exit
        else evaluate_entries(session, quote, now=now, broker=broker, day_start_utc=day_start_utc)
    )
    closed = evaluate_exits(session, quote, now=now, broker=broker, force=force_exit)
    return f"opened={len(opened)} closed={len(closed)}"
