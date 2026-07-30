/**
 * TRADER tab data — the read-only Alpaca paper account, positions, equity curve,
 * and trade blotter, all served by the prediction API (:8001) so keys never reach
 * the browser. Every fetcher is fail-soft: a network miss returns
 * `reachable:false`, a missing-keys backend returns `configured:false`, and an
 * upstream vendor hiccup returns `available:false` — the UI renders an honest
 * empty/degraded state for each rather than throwing.
 */
import { PRED_API } from "./config";

/** News provenance joined from OUR DB onto a fill/position (null when the Alpaca
 *  order id doesn't resolve to a sim_trade — honest, never guessed). */
export interface Provenance {
  trade_id: string;
  config_id: string;
  config_name: string | null;
  entry_source: string | null;
  notional: number | null;
  cluster_id: string | null;
  catalyst_type: string | null;
  high_alert: boolean;
  headline: string | null;
  url: string | null;
  source: string | null;
  source_class: string | null;
}

export interface MarketClock {
  is_open: boolean;
  timestamp: string | null;
  next_open: string | null;
  next_close: string | null;
}

export interface TraderAccount {
  configured: boolean;
  available: boolean;
  reachable: boolean;
  account_mask: string | null;
  status: string | null;
  currency: string | null;
  equity: number | null;
  last_equity: number | null;
  cash: number | null;
  buying_power: number | null;
  portfolio_value: number | null;
  long_market_value: number | null;
  short_market_value: number | null;
  day_pl: number | null;
  day_pl_pct: number | null;
  pattern_day_trader: boolean;
  trading_blocked: boolean;
  account_blocked: boolean;
  clock: MarketClock;
}

export interface EquityPoint {
  t: number; // epoch seconds
  equity: number | null;
  pl: number | null;
  pl_pct: number | null;
}

export interface PortfolioHistory {
  configured: boolean;
  available: boolean;
  reachable: boolean;
  base_value: number | null;
  timeframe: string | null;
  points: EquityPoint[];
}

export interface Position {
  ticker: string;
  side: string | null;
  qty: number | null;
  avg_entry_price: number | null;
  current_price: number | null;
  market_value: number | null;
  cost_basis: number | null;
  unrealized_pl: number | null;
  unrealized_pl_pct: number | null;
  change_today: number | null;
  provenance: Provenance | null;
}

export interface PositionsResult {
  configured: boolean;
  available: boolean;
  reachable: boolean;
  count: number;
  items: Position[];
}

export interface RoundTrip {
  ticker: string;
  direction: number; // 1 long, -1 short
  qty: number;
  entry_price: number;
  entry_time: string | null;
  entry_order_id: string | null;
  exit_price: number;
  exit_time: string | null;
  exit_order_id: string | null;
  realized_pl: number;
  realized_pl_pct: number | null;
  provenance: Provenance | null;
}

export interface BlotterResult {
  configured: boolean;
  available: boolean;
  reachable: boolean;
  scope: string;
  count: number;
  items: RoundTrip[];
}

export type BlotterScope = "closed" | "open" | "today" | "all";

/** Timeframe presets for the equity curve — (label, Alpaca period, timeframe). */
export const CURVE_TIMEFRAMES: { label: string; period: string; timeframe: string }[] = [
  { label: "1D", period: "1D", timeframe: "5Min" },
  { label: "1W", period: "1W", timeframe: "1H" },
  { label: "1M", period: "1M", timeframe: "1D" },
  { label: "3M", period: "3M", timeframe: "1D" },
  { label: "1Y", period: "1A", timeframe: "1D" },
  { label: "ALL", period: "all", timeframe: "1D" },
];

async function getJson<T>(path: string, fallback: T): Promise<T & { reachable: boolean }> {
  try {
    const res = await fetch(`${PRED_API}${path}`, { cache: "no-store" });
    if (!res.ok) return { ...fallback, reachable: false };
    const body = await res.json();
    return {
      // available defaults true when the backend returns data without the flag
      available: true,
      ...body,
      reachable: true,
    };
  } catch {
    return { ...fallback, reachable: false };
  }
}

export function fetchTraderAccount(): Promise<TraderAccount> {
  return getJson<TraderAccount>("/trader/account", {
    configured: false,
    available: false,
    reachable: false,
    account_mask: null,
    status: null,
    currency: null,
    equity: null,
    last_equity: null,
    cash: null,
    buying_power: null,
    portfolio_value: null,
    long_market_value: null,
    short_market_value: null,
    day_pl: null,
    day_pl_pct: null,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    clock: { is_open: false, timestamp: null, next_open: null, next_close: null },
  });
}

export function fetchPortfolioHistory(period: string, timeframe: string): Promise<PortfolioHistory> {
  const p = new URLSearchParams({ period, timeframe });
  return getJson<PortfolioHistory>(`/trader/portfolio/history?${p}`, {
    configured: false,
    available: false,
    reachable: false,
    base_value: null,
    timeframe: null,
    points: [],
  });
}

export function fetchPositions(): Promise<PositionsResult> {
  return getJson<PositionsResult>("/trader/positions", {
    configured: false,
    available: false,
    reachable: false,
    count: 0,
    items: [],
  });
}

export function fetchBlotter(scope: BlotterScope, configId?: string): Promise<BlotterResult> {
  const p = new URLSearchParams({ scope });
  if (configId) p.set("config_id", configId);
  // ET calendar date drives the "today" filter server-side.
  if (scope === "today") p.set("today_et", etToday());
  return getJson<BlotterResult>(`/trader/blotter?${p}`, {
    configured: false,
    available: false,
    reachable: false,
    scope,
    count: 0,
    items: [],
  });
}

// --- formatting helpers (shared across the TRADER components) --------------
/** $ with thousands separators; `dp` decimals. em-dash for null. */
export function fmtUsd(n: number | null | undefined, dp = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

/** Signed $ (+/-) for P&L cells. */
export function fmtSignedUsd(n: number | null | undefined, dp = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  const s = fmtUsd(Math.abs(n), dp);
  return n < 0 ? `-${s}` : `+${s}`;
}

/** Fraction -> signed percent string (0.0123 -> "+1.23%"). */
export function fmtPct(n: number | null | undefined, dp = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(dp)}%`;
}

/** ISO -> "MM-DD HH:MM" compact stamp, em-dash for null. */
export function fmtStamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.slice(5, 16).replace("T", " ");
}

/** Guard: only render http(s) links (never javascript:/data: from provenance). */
export function isHttp(url: string | null | undefined): url is string {
  return !!url && (url.startsWith("http://") || url.startsWith("https://"));
}

/** Today's date in US/Eastern as YYYY-MM-DD (the market calendar's day). */
export function etToday(): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  return parts; // en-CA gives YYYY-MM-DD
}
