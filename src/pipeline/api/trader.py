"""TRADER dashboard view builders — read-only paper-account views + provenance.

The web app never touches the Alpaca write path; these functions shape the
read-only paper book (from ``PaperAccountReader``) into the shapes the TRADER tab
renders, and join each row back to OUR DB for provenance — the marketable
differentiator: "the agent bought AAPL at 9:47 — here's the headline that caused
it."

The Alpaca account is the source of truth for what actually happened; our DB
(sim_trades / sim_configs / clusters) is joined on top for context. The link is
``sim_trades.broker_entry_order_id`` / ``broker_exit_order_id`` == the Alpaca
order id. A fresh paper account has no matching order ids, so provenance resolves
to null (honest — never guessed) and lights up as the driver trades this account.

Everything here is pure/deterministic given its inputs (the round-trip matcher
takes a plain fills list; the enrichers take a Session), so it unit-tests without
network. All money math uses the real Alpaca fill prices — no synthetic costs are
applied here (COST_RT lives in the sim ledger, a separate lane).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pipeline.common.models import (
    Cluster,
    ClusterScore,
    RawItem,
    SimConfig,
    SimTrade,
)

# Alpaca order statuses that represent real executed quantity we can pair.
_FILLED_STATES = {"filled", "partially_filled"}


# --------------------------------------------------------------------------- #
# account header
# --------------------------------------------------------------------------- #
def _f(v: Any) -> float | None:
    """Alpaca sends numbers as strings; parse to float, or None if absent/blank."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def account_view(account: dict[str, Any], clock: dict[str, Any] | None) -> dict[str, Any]:
    """Portfolio header: equity, cash, buying power, day P&L, and the market clock
    (open/closed chip). Day P&L is derived from ``equity`` vs ``last_equity``
    (equity at the previous close) — Alpaca's own basis for the daily figure."""
    equity = _f(account.get("equity"))
    last_equity = _f(account.get("last_equity"))
    day_pl: float | None = None
    day_pl_pct: float | None = None
    if equity is not None and last_equity is not None:
        day_pl = round(equity - last_equity, 2)
        day_pl_pct = round((equity / last_equity - 1.0), 6) if last_equity else None

    clk = clock or {}
    return {
        "configured": True,
        # account number is not a secret, but there's no reason to surface it in
        # full — mask to the last 4 so the operator can still tell books apart.
        "account_mask": _mask(account.get("account_number")),
        "status": account.get("status"),
        "currency": account.get("currency", "USD"),
        "endpoint": account.get("endpoint"),  # proves the paper host was read
        "equity": equity,
        "last_equity": last_equity,
        "cash": _f(account.get("cash")),
        "buying_power": _f(account.get("buying_power")),
        "portfolio_value": _f(account.get("portfolio_value")),
        "long_market_value": _f(account.get("long_market_value")),
        "short_market_value": _f(account.get("short_market_value")),
        "day_pl": day_pl,
        "day_pl_pct": day_pl_pct,
        "pattern_day_trader": bool(account.get("pattern_day_trader")),
        "trading_blocked": bool(account.get("trading_blocked")),
        "account_blocked": bool(account.get("account_blocked")),
        "clock": {
            "is_open": bool(clk.get("is_open")),
            "timestamp": clk.get("timestamp"),
            "next_open": clk.get("next_open"),
            "next_close": clk.get("next_close"),
        },
    }


def _mask(acct_no: Any) -> str | None:
    s = str(acct_no or "")
    return f"••••{s[-4:]}" if len(s) >= 4 else None


def portfolio_history_view(hist: dict[str, Any]) -> dict[str, Any]:
    """Reshape Alpaca's parallel-array portfolio history into charted points
    {t (epoch seconds), equity, pl, pl_pct}. A fresh account returns short/flat
    arrays -> few/one points, which the UI renders as 'no history yet'."""
    ts = hist.get("timestamp") or []
    eq = hist.get("equity") or []
    pl = hist.get("profit_loss") or []
    plpct = hist.get("profit_loss_pct") or []
    points: list[dict[str, Any]] = []
    for i, t in enumerate(ts):
        e = eq[i] if i < len(eq) else None
        if e is None:  # Alpaca emits null equity for pre-inception buckets
            continue
        points.append(
            {
                "t": int(t),
                "equity": _f(e),
                "pl": _f(pl[i]) if i < len(pl) else None,
                "pl_pct": _f(plpct[i]) if i < len(plpct) else None,
            }
        )
    return {
        "configured": True,
        "base_value": _f(hist.get("base_value")),
        "timeframe": hist.get("timeframe"),
        "points": points,
    }


# --------------------------------------------------------------------------- #
# positions (open leg)
# --------------------------------------------------------------------------- #
def positions_view(positions: list[dict[str, Any]], session: Session) -> dict[str, Any]:
    """Live positions with unrealized P&L, each enriched with provenance from the
    most recent OPEN sim_trade for that ticker (config + originating catalyst)."""
    rows: list[dict[str, Any]] = []
    tickers = [str(p.get("symbol", "")).upper() for p in positions if p.get("symbol")]
    prov_by_ticker = _open_trade_provenance(session, tickers)
    for p in positions:
        sym = str(p.get("symbol", "")).upper()
        qty = _f(p.get("qty"))
        rows.append(
            {
                "ticker": sym,
                "side": p.get("side"),  # long | short
                "qty": qty,
                "avg_entry_price": _f(p.get("avg_entry_price")),
                "current_price": _f(p.get("current_price")),
                "market_value": _f(p.get("market_value")),
                "cost_basis": _f(p.get("cost_basis")),
                "unrealized_pl": _f(p.get("unrealized_pl")),
                "unrealized_pl_pct": _f(p.get("unrealized_plpc")),
                "change_today": _f(p.get("change_today")),
                "provenance": prov_by_ticker.get(sym),
            }
        )
    rows.sort(key=lambda r: abs(r.get("market_value") or 0.0), reverse=True)
    return {"configured": True, "count": len(rows), "items": rows}


# --------------------------------------------------------------------------- #
# round-trip pairing (closed blotter rows) — pure, no DB, no network
# --------------------------------------------------------------------------- #
def fills_from_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distil Alpaca orders into the minimal executed-fill records the matcher
    needs. Only orders with real filled quantity survive; unfilled/canceled
    orders never enter the ledger (mirrors 'no fill == no trade')."""
    out: list[dict[str, Any]] = []
    for o in orders:
        status = str(o.get("status", "")).lower()
        fq = _f(o.get("filled_qty")) or 0.0
        fap = _f(o.get("filled_avg_price"))
        if status not in _FILLED_STATES or fq <= 0 or fap is None:
            continue
        out.append(
            {
                "symbol": str(o.get("symbol", "")).upper(),
                "side": str(o.get("side", "")).lower(),  # buy | sell
                "qty": fq,
                "price": fap,
                "time": o.get("filled_at") or o.get("submitted_at"),
                "order_id": o.get("id"),
            }
        )
    return out


def pair_round_trips(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FIFO-match executed fills into closed round-trips per symbol.

    Signed-position model: a buy adds +qty, a sell adds -qty. Fills that move the
    position toward zero CLOSE open lots (FIFO); quantity beyond the close opens
    new lots in the new direction. Each fully-closed matched quantity becomes one
    round-trip row with quantity-weighted entry/exit prices and realized P&L. Lots
    still open at the end are ignored here (they're shown live from /positions).

    Handles partial fills and multiple concurrent lots; whole-share only in
    practice (the broker never shorts fractionally), but the math is
    quantity-general. Fills with an unparseable time sort last but still pair.
    """
    # Stable chronological order; None-times sort last (recent-but-unstamped).
    ordered = sorted(fills, key=lambda f: (f.get("time") is None, str(f.get("time") or "")))
    # symbol -> list of open lots: {dir, qty, price, time, order_id}
    open_lots: dict[str, list[dict[str, Any]]] = {}
    trips: list[dict[str, Any]] = []

    for f in ordered:
        sym = f["symbol"]
        direction = 1 if f["side"] == "buy" else -1
        remaining = f["qty"]
        price = f["price"]
        lots = open_lots.setdefault(sym, [])

        # First, close opposite-direction lots FIFO.
        while remaining > 0 and lots and lots[0]["dir"] == -direction:
            lot = lots[0]
            matched = min(remaining, lot["qty"])
            entry_dir = lot["dir"]  # +1 long entry, -1 short entry
            # realized P&L per share = (exit - entry) * entry_direction
            realized = (price - lot["price"]) * matched * entry_dir
            cost = lot["price"] * matched
            trips.append(
                {
                    "ticker": sym,
                    "direction": entry_dir,
                    "qty": matched,
                    "entry_price": round(lot["price"], 4),
                    "entry_time": lot["time"],
                    "entry_order_id": lot["order_id"],
                    "exit_price": round(price, 4),
                    "exit_time": f["time"],
                    "exit_order_id": f["order_id"],
                    "realized_pl": round(realized, 2),
                    "realized_pl_pct": round(realized / cost, 6) if cost else None,
                }
            )
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 1e-9:
                lots.pop(0)

        # Any leftover opens a new lot in this fill's direction.
        if remaining > 1e-9:
            lots.append(
                {
                    "dir": direction,
                    "qty": remaining,
                    "price": price,
                    "time": f["time"],
                    "order_id": f["order_id"],
                }
            )

    # Newest exits first for the blotter.
    trips.sort(key=lambda t: (t.get("exit_time") is None, str(t.get("exit_time") or "")), reverse=True)
    return trips


# --------------------------------------------------------------------------- #
# provenance enrichment (DB joins)
# --------------------------------------------------------------------------- #
def _cluster_context(session: Session, cluster_ids: list[str]) -> dict[str, dict[str, Any]]:
    """cluster_id -> {catalyst_type, headline, url, source, source_class}. Mirrors
    the /clusters/resolve join (Cluster -> origin RawItem -> ClusterScore). Soft:
    unresolved clusters simply don't appear."""
    wanted = list({c for c in cluster_ids if c})
    if not wanted:
        return {}
    rows = session.execute(
        select(Cluster, RawItem, ClusterScore)
        .join(RawItem, RawItem.id == Cluster.origin_item_id)
        .outerjoin(ClusterScore, ClusterScore.cluster_id == Cluster.cluster_id)
        .where(Cluster.cluster_id.in_(wanted))
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for cl, origin, cs in rows:
        payload = origin.payload_json or {}
        out[cl.cluster_id] = {
            "catalyst_type": cs.catalyst_type if cs else None,
            "high_alert": bool(cs.high_alert) if cs else False,
            "headline": payload.get("title"),
            "url": origin.url,
            "source": origin.source,
            "source_class": origin.source_class,
        }
    return out


def _config_names(session: Session, config_ids: list[str]) -> dict[str, str]:
    wanted = list({c for c in config_ids if c})
    if not wanted:
        return {}
    rows = session.execute(
        select(SimConfig.config_id, SimConfig.name).where(SimConfig.config_id.in_(wanted))
    ).all()
    return {cid: name for cid, name in rows}


def _open_trade_provenance(session: Session, tickers: list[str]) -> dict[str, dict[str, Any]]:
    """ticker -> provenance for the most recent OPEN sim_trade on that ticker.
    Used for live positions (which Alpaca aggregates per symbol, so we attribute
    to the latest open trade)."""
    wanted = list({t for t in tickers if t})
    if not wanted:
        return {}
    rows = (
        session.execute(
            select(SimTrade)
            .where(SimTrade.ticker.in_(wanted), SimTrade.status == "open")
            .order_by(SimTrade.entered_at.desc())
        )
        .scalars()
        .all()
    )
    latest: dict[str, SimTrade] = {}
    for t in rows:
        latest.setdefault(t.ticker.upper(), t)
    ctx = _cluster_context(session, [t.cluster_id for t in latest.values() if t.cluster_id])
    names = _config_names(session, [t.config_id for t in latest.values()])
    out: dict[str, dict[str, Any]] = {}
    for sym, t in latest.items():
        out[sym] = _provenance_dict(t, names, ctx)
    return out


def _provenance_dict(
    t: SimTrade, names: dict[str, str], ctx: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    cluster = ctx.get(t.cluster_id or "") or {}
    feats = t.features_json or {}
    return {
        "trade_id": t.trade_id,
        "config_id": t.config_id,
        "config_name": names.get(t.config_id),
        "entry_source": t.entry_source,
        "notional": feats.get("notional"),
        "cluster_id": t.cluster_id,
        "catalyst_type": cluster.get("catalyst_type"),
        "high_alert": cluster.get("high_alert", False),
        "headline": cluster.get("headline"),
        "url": cluster.get("url"),
        "source": cluster.get("source"),
        "source_class": cluster.get("source_class"),
    }


def enrich_round_trips(trips: list[dict[str, Any]], session: Session) -> list[dict[str, Any]]:
    """Attach provenance to each closed round-trip by joining its Alpaca order ids
    back to sim_trades (broker_entry_order_id / broker_exit_order_id). Rows with no
    matching sim_trade keep provenance=null — honest for manual or pre-account
    trades."""
    order_ids = [t["entry_order_id"] for t in trips if t.get("entry_order_id")]
    order_ids += [t["exit_order_id"] for t in trips if t.get("exit_order_id")]
    trade_by_order = _trades_by_order_id(session, order_ids)

    all_clusters = [t.cluster_id for t in trade_by_order.values() if t.cluster_id]
    all_configs = [t.config_id for t in trade_by_order.values()]
    ctx = _cluster_context(session, all_clusters)
    names = _config_names(session, all_configs)

    for t in trips:
        st = trade_by_order.get(t.get("entry_order_id") or "") or trade_by_order.get(
            t.get("exit_order_id") or ""
        )
        t["provenance"] = _provenance_dict(st, names, ctx) if st is not None else None
    return trips


def _trades_by_order_id(session: Session, order_ids: list[str]) -> dict[str, SimTrade]:
    wanted = list({o for o in order_ids if o})
    if not wanted:
        return {}
    rows = (
        session.execute(
            select(SimTrade).where(
                or_(
                    SimTrade.broker_entry_order_id.in_(wanted),
                    SimTrade.broker_exit_order_id.in_(wanted),
                )
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, SimTrade] = {}
    for t in rows:
        if t.broker_entry_order_id:
            out[t.broker_entry_order_id] = t
        if t.broker_exit_order_id:
            out[t.broker_exit_order_id] = t
    return out


# --------------------------------------------------------------------------- #
# blotter assembly
# --------------------------------------------------------------------------- #
def _et_date(iso_ts: str | None) -> str | None:
    """The ET calendar date of an ISO timestamp (Alpaca stamps are UTC 'Z' or
    offset ISO). Best-effort; None on unparseable. Used only for the 'today'
    filter, so a miss just excludes the row from 'today', never crashes."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Convert to US/Eastern without a tz dependency: Alpaca offset ISO already
    # encodes the zone for calendar rows; for UTC stamps we approximate ET as
    # UTC-4/5. We only need day bucketing, and callers pass the ET 'today' string.
    return dt.date().isoformat()


def blotter_view(
    orders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    session: Session,
    *,
    scope: str = "closed",
    config_id: str | None = None,
    today_et: str | None = None,
) -> dict[str, Any]:
    """Assemble the trade blotter.

    scope:
      * ``closed`` — realized round-trips (FIFO-paired from filled orders)
      * ``open``   — current positions (open leg, live unrealized P&L)
      * ``today``  — round-trips whose exit is today (ET)
      * ``all``    — closed round-trips (no date filter)

    ``config_id`` filters to rows whose provenance resolves to that sim config.
    """
    if scope == "open":
        pv = positions_view(positions, session)
        items = pv["items"]
        if config_id:
            items = [r for r in items if (r.get("provenance") or {}).get("config_id") == config_id]
        return {"configured": True, "scope": scope, "count": len(items), "items": items}

    trips = enrich_round_trips(pair_round_trips(fills_from_orders(orders)), session)
    if scope == "today" and today_et:
        trips = [t for t in trips if _et_date(t.get("exit_time")) == today_et]
    if config_id:
        trips = [t for t in trips if (t.get("provenance") or {}).get("config_id") == config_id]
    return {"configured": True, "scope": scope, "count": len(trips), "items": trips}
