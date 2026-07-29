/**
 * Ticker chart client (PRED_API :8001, /tickers/{t}/series). Price bars from the
 * parquet cache + the daily attention series (news volume, sentiment, buzz-z).
 * Price is the floor every ticker has; attention/buzz appear where data exists.
 */
import { PRED_API } from "@/lib/config";

export interface PricePoint {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export interface ClusterItem {
  cluster_id: string;
  title: string | null;
  source: string;
  source_class: string;
  url: string | null;
  published_at: string;
  member_count: number;
  catalyst_type: string | null;
  finbert_score: number | null;
  materiality: number | null;
  high_alert: boolean;
  /** When the system first scored/classified the cluster (the call time);
   * null for unscored clusters or rows persisted before tracking. */
  called_at: string | null;
}

export interface AttentionPoint {
  date: string;
  struct: number;
  social: number;
  sentiment: number | null;
  buzz_z: number | null;
}

export interface TickerSeries {
  ticker: string;
  baseline: { mean: number; std: number; n_days: number; source: string } | null;
  price: PricePoint[];
  attention: AttentionPoint[];
}

export async function fetchTickerSeries(
  ticker: string,
  days = 120,
): Promise<TickerSeries | null> {
  try {
    const res = await fetch(
      `${PRED_API}/tickers/${encodeURIComponent(ticker.toUpperCase())}/series?days=${days}`,
      { cache: "no-store" },
    );
    if (!res.ok) return null;
    return (await res.json()) as TickerSeries;
  } catch {
    return null;
  }
}

export async function fetchTickerClusters(ticker: string, limit = 30): Promise<ClusterItem[]> {
  try {
    const res = await fetch(
      `${PRED_API}/tickers/${encodeURIComponent(ticker.toUpperCase())}/clusters?limit=${limit}`,
      { cache: "no-store" },
    );
    if (!res.ok) return [];
    return ((await res.json()).items ?? []) as ClusterItem[];
  } catch {
    return [];
  }
}

/** Real intraday candles (yfinance 1m/5m/15m, TTL-cached server-side). Times are
 * ET-shifted epochs; `extended` marks pre/after-hours bars. available=false ->
 * the UI keeps the last-close fallback (no synthetic bars). */
export interface IntradayBar {
  time: number; // epoch seconds, ET-shifted for chart display
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  extended: boolean;
}

export interface IntradayBars {
  available: boolean;
  interval: string | null; // "1m" | "5m" | "15m"
  bars: IntradayBar[];
}

export async function fetchIntradayBars(
  ticker: string,
  window: "1d" | "1w",
): Promise<IntradayBars> {
  try {
    const res = await fetch(
      `${PRED_API}/tickers/${encodeURIComponent(ticker.toUpperCase())}/intraday/bars?window=${window}`,
      { cache: "no-store" },
    );
    if (!res.ok) return { available: false, interval: null, bars: [] };
    const b = await res.json();
    return { available: !!b.available, interval: b.interval ?? null, bars: b.bars ?? [] };
  } catch {
    return { available: false, interval: null, bars: [] };
  }
}

/** On-demand HOURLY Google-Trends search interest (own-term relative 0-100, NOT
 * counts), fetched per viewed ticker only (server TTL-cached, fail-soft). source
 * 'unavailable' with empty points -> the panel shows an honest label, no fake
 * bars. Each point.hour is a UTC ISO timestamp. */
export interface SearchHourlyPoint {
  hour: string;
  value: number;
}

export interface SearchHourly {
  ticker: string;
  source: "google_trends" | "unavailable";
  label: string;
  points: SearchHourlyPoint[];
}

export async function fetchSearchHourly(ticker: string, hours = 48): Promise<SearchHourly> {
  const empty: SearchHourly = {
    ticker: ticker.toUpperCase(),
    source: "unavailable",
    label: "relative interest (0-100, own-term)",
    points: [],
  };
  try {
    const res = await fetch(
      `${PRED_API}/tickers/${encodeURIComponent(ticker.toUpperCase())}/search-interest/hourly?hours=${hours}`,
      { cache: "no-store" },
    );
    if (!res.ok) return empty;
    const b = await res.json();
    return {
      ticker: b.ticker ?? empty.ticker,
      source: b.source === "google_trends" ? "google_trends" : "unavailable",
      label: b.label ?? empty.label,
      points: Array.isArray(b.points) ? b.points : [],
    };
  } catch {
    return empty;
  }
}

/** Live Redis intraday counters (mentions/hour written at ingest). live=false
 * when Redis is unavailable — the panel falls back to client-side bucketing. */
export interface IntradayLive {
  live: boolean;
  items: { hour: string; count: number }[];
}

export async function fetchIntradayLive(ticker: string): Promise<IntradayLive> {
  try {
    const res = await fetch(
      `${PRED_API}/tickers/${encodeURIComponent(ticker.toUpperCase())}/intraday/live`,
      { cache: "no-store" },
    );
    if (!res.ok) return { live: false, items: [] };
    const b = await res.json();
    return { live: !!b.live, items: b.items ?? [] };
  } catch {
    return { live: false, items: [] };
  }
}
