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
        "created_at": account.get("created_at"),
        "inception_date": _et_date(account.get("created_at")),
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
try:  # stdlib on 3.9+; the app targets 3.12
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 — never fatal; fall back to naive UTC dates
    _ET = None  # type: ignore[assignment]


def _et_date(iso_ts: str | None) -> str | None:
    """The ET calendar date (YYYY-MM-DD) of an ISO timestamp. Alpaca stamps are
    UTC 'Z' or offset ISO; we convert to US/Eastern so a fill just after the open
    or just before the close buckets on the correct trading day. None on
    unparseable (the row is simply excluded from a date bucket, never crashes)."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if _ET is not None and dt.tzinfo is not None:
        dt = dt.astimezone(_ET)
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


# --------------------------------------------------------------------------- #
# Phase 2: history + context (P&L calendar, day detail, chart markers)
# --------------------------------------------------------------------------- #
def calendar_view(orders: list[dict[str, Any]], session: Session) -> dict[str, Any]:
    """Per-day realized P&L for a month grid, bucketed by each round-trip's exit
    ET date. A fresh account yields an empty ``days`` map, which the UI renders as
    an intentional blank calendar (never an error)."""
    trips = enrich_round_trips(pair_round_trips(fills_from_orders(orders)), session)
    days: dict[str, dict[str, Any]] = {}
    for t in trips:
        d = _et_date(t.get("exit_time"))
        if d is None:
            continue
        cell = days.setdefault(d, {"realized_pl": 0.0, "trips": 0, "wins": 0, "losses": 0})
        cell["realized_pl"] = round(cell["realized_pl"] + (t.get("realized_pl") or 0.0), 2)
        cell["trips"] += 1
        if (t.get("realized_pl") or 0.0) >= 0:
            cell["wins"] += 1
        else:
            cell["losses"] += 1
    return {"configured": True, "days": days}


def day_view(
    orders: list[dict[str, Any]],
    session: Session,
    *,
    date: str,
    account_inception: str | None = None,
) -> dict[str, Any]:
    """One day's detail: this account's round-trips exited on ``date`` (ET) + any
    EOD report cards from sim_daily_summary for that session.

    HONESTY: report cards are keyed by session_date across ALL history, so a card
    dated before this paper account existed is from the PRIOR portfolio. We flag
    each card ``prior_account`` when its date precedes ``account_inception`` so the
    UI can label it truthfully rather than implying the current book produced it."""
    trips = [
        t
        for t in enrich_round_trips(pair_round_trips(fills_from_orders(orders)), session)
        if _et_date(t.get("exit_time")) == date
    ]
    cards = _report_cards_for_date(session, date, account_inception)
    return {
        "configured": True,
        "date": date,
        "round_trips": trips,
        "report_cards": cards,
        "account_inception": account_inception,
    }


def _report_cards_for_date(
    session: Session, date: str, account_inception: str | None
) -> list[dict[str, Any]]:
    """sim_daily_summary rows for a session date, each flagged prior_account when
    it predates this paper account. Returns [] (never raises) if the rollup table
    doesn't exist yet."""
    from pipeline.common.models import SimDailySummary

    try:
        rows = (
            session.execute(
                select(SimDailySummary)
                .where(SimDailySummary.session_date == date)
                .order_by(SimDailySummary.config_name)
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 — table may not exist until the driver's first EOD
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        sd = r.session_date.isoformat() if hasattr(r.session_date, "isoformat") else str(r.session_date)
        out.append(
            {
                "session_date": sd,
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
                # honest provenance label
                "prior_account": bool(account_inception and sd < account_inception),
            }
        )
    return out


def markers_view(orders: list[dict[str, Any]], ticker: str, session: Session) -> dict[str, Any]:
    """Entry/exit fills for ONE ticker, shaped for chart markers on the ticker
    page's candle chart. Each marker: {time (epoch s), price, side (buy|sell),
    kind (entry|exit), qty}. Kind is inferred by FIFO-pairing this ticker's fills
    so a marker reads as opening or closing a position."""
    tk = ticker.upper()
    fills = [f for f in fills_from_orders(orders) if f["symbol"] == tk]
    if not fills:
        return {"configured": True, "ticker": tk, "markers": []}
    # Re-derive entry/exit role via the same signed-FIFO logic the blotter uses.
    ordered = sorted(fills, key=lambda f: (f.get("time") is None, str(f.get("time") or "")))
    position = 0.0
    markers: list[dict[str, Any]] = []
    for f in ordered:
        signed = f["qty"] if f["side"] == "buy" else -f["qty"]
        # A fill that moves |position| toward zero is (at least partly) an exit.
        kind = "exit" if position != 0 and (position > 0) != (signed > 0) else "entry"
        markers.append(
            {
                "time": _epoch(f.get("time")),
                "date": _et_date(f.get("time")),  # ET trading date, for daily-chart alignment
                "price": f["price"],
                "side": f["side"],
                "kind": kind,
                "qty": f["qty"],
            }
        )
        position += signed
    markers = [m for m in markers if m["time"] is not None]
    markers.sort(key=lambda m: m["time"])
    return {"configured": True, "ticker": tk, "markers": markers}


def _epoch(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        return int(datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None
