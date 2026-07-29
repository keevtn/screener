import { PRED_API } from "@/lib/config";
import { heat } from "@/lib/buzz";
import { fetchPredictionMap, LivePrediction } from "@/lib/predictions";
import { fetchPrices } from "@/lib/prices";
import { EventKind, ScreenerRow, SignalDirection } from "./types";
import type { StatsMap, TickerStats } from "./screenerStats";

/**
 * Load the SCREENER row set from the prediction API's /screener/rows endpoint.
 *
 * Rows are one-per-UNIVERSE-ticker with recent attributed coverage — sourced from
 * our own SQLite plane (cluster attributions + scores + attention rollup +
 * fundamentals), NOT the shallow Mongo /api/news window the LIVE tape reads. This
 * is what makes previously-invisible universe names (e.g. ATAI's M&A) appear the
 * moment they have attributed coverage. Live prices are overlaid client-side from
 * the cache-only /marketdata/prices endpoint; there is no demo fallback — an
 * unreachable API yields honest empty/error state.
 */

export interface ScreenerData {
  rows: ScreenerRow[];
  /** vs-own-history stats keyed by ticker (folded from the rows endpoint). */
  stats: StatsMap;
  /** Denominator for the "matched / total" counter = in-window universe tickers. */
  universe: number;
  /** The window the rows cover, in hours. */
  windowHours: number;
  /** true = rows came from the live endpoint; false = API unreachable (empty state). */
  live: boolean;
  /** true = the API responded but returned zero rows (honest empty, not an error). */
  reachable: boolean;
}

/** One row as served by GET /screener/rows. */
interface RawScreenerRow {
  ticker: string;
  mentions: number;
  sources: number;
  finbert_mean: number | null;
  finbert_latest: number | null;
  lm_mean: number | null;
  lm_latest: number | null;
  latest_at: string | null;
  catalyst_in_window: boolean;
  high_alert: boolean;
  buzz_z: number | null;
  last_catalyst: {
    catalyst_type: string;
    event_stage: string | null;
    high_alert: boolean;
    published_at: string;
    called_at: string | null;
    age_hours: number;
  } | null;
  fundamentals: {
    name: string | null;
    sector: string | null;
    industry: string | null;
    market_cap: number | null;
    avg_volume: number | null;
    short_float: number | null;
    beta: number | null;
  } | null;
  stats: TickerStats | null;
}

interface RawScreenerResponse {
  window_hours: number;
  count: number;
  rows: RawScreenerRow[];
}

/** Map a catalyst_type to the coarse EVENT-group tag the grid renders. */
function eventKindFromCatalyst(catalyst: string): EventKind {
  if (catalyst === "fda_action") return "FDA";
  if (catalyst === "ma") return "M&A";
  if (catalyst === "earnings_results") return "EARN";
  if (catalyst === "secondary_offering" || catalyst === "insider_cluster") return "SEC";
  return "NEWS";
}

/** Short human label for a catalyst, e.g. "M&A · announced". */
function catalystLabel(kind: EventKind, catalyst: string, stage: string | null): string {
  const detail = stage ?? catalyst.replace(/_/g, " ");
  return `${kind ?? "NEWS"} · ${detail}`;
}

function toRow(r: RawScreenerRow, predictions: Map<string, LivePrediction>): ScreenerRow {
  // finbert is the primary tone the grid's SENT column shows (I7: lm is carried
  // separately in the endpoint for the deeper axes, never pre-blended in).
  const sentiment = r.finbert_mean ?? 0;

  // Signal: real /predictions overlaid where present, else direction inferred from
  // sentiment sign (confidence/config left null — not faked).
  const pred = predictions.get(r.ticker);
  const sentDir: SignalDirection =
    sentiment > 0.15 ? "bullish" : sentiment < -0.15 ? "bearish" : "none";
  const signal = pred
    ? { direction: pred.direction, confidence: pred.confidence, config: pred.config_version }
    : { direction: sentDir, confidence: null, config: null };

  const cat = r.last_catalyst;
  const kind = cat ? eventKindFromCatalyst(cat.catalyst_type) : null;
  const f = r.fundamentals;

  return {
    ticker: r.ticker,
    name: f?.name ?? r.ticker,
    signal,
    attention: {
      sentiment: Number(sentiment.toFixed(2)),
      heat: Number(heat(sentiment, r.mentions).toFixed(3)), // signed sentiment x density
      buzzZ: r.buzz_z,
      mentions: r.mentions,
      authors: r.sources,
    },
    event: {
      material: cat != null && (cat.high_alert || r.catalyst_in_window),
      kind,
      label: cat ? catalystLabel(kind, cat.catalyst_type, cat.event_stage) : null,
      ageHours: cat ? Number(cat.age_hours.toFixed(1)) : null,
    },
    price: { last: null, pctChange: null, volOverAvg: null }, // overlaid from /marketdata/prices
    fundamentals: f
      ? {
          name: f.name,
          sector: f.sector,
          industry: f.industry,
          marketCap: f.market_cap,
          avgVolume: f.avg_volume,
          shortFloat: f.short_float,
          beta: f.beta,
        }
      : null,
    provenance: {
      signal: pred ? "live" : "derived",
      attention: "live",
      event: cat ? "live" : "derived",
      price: "demo", // upgraded to "live" once a cache quote overlays below
    },
  };
}

/** Load screener rows for a window (hours). Honest empty/error state on failure —
 * no demo fallback (that was retired with the Mongo-window derivation). */
export async function loadScreenerData(hours = 48): Promise<ScreenerData> {
  let body: RawScreenerResponse;
  try {
    const res = await fetch(`${PRED_API}/screener/rows?hours=${hours}`, { cache: "no-store" });
    if (!res.ok) {
      return { rows: [], stats: {}, universe: 0, windowHours: hours, live: false, reachable: false };
    }
    body = (await res.json()) as RawScreenerResponse;
  } catch {
    // prediction API unreachable -> honest empty state, never demo rows.
    return { rows: [], stats: {}, universe: 0, windowHours: hours, live: false, reachable: false };
  }

  const predictions = await fetchPredictionMap();
  const rows = (body.rows ?? []).map((r) => toRow(r, predictions));
  const stats: StatsMap = {};
  for (const r of body.rows ?? []) {
    if (r.stats) stats[r.ticker] = r.stats;
  }

  // Overlay real cache prices where available (uncached tickers render "—").
  const tickers = rows.map((r) => r.ticker);
  const prices = await fetchPrices(tickers);
  for (const row of rows) {
    const q = prices.get(row.ticker);
    if (q) {
      row.price = { last: q.last, pctChange: q.pct_change, volOverAvg: q.vol_over_avg };
      row.provenance = { ...row.provenance, price: "live" };
    }
  }

  return {
    rows,
    stats,
    universe: body.count ?? rows.length,
    windowHours: body.window_hours ?? hours,
    live: true,
    reachable: true,
  };
}
