/**
 * TAPE_ Screener data model.
 *
 * A screener row is one *ticker-level* aggregated signal state — distinct from
 * the article-level `NewsItem` the /api/news endpoint serves today. Most of
 * these fields have no backend path yet; each field-group carries a
 * `Provenance` marker so the UI can be honest about live vs derived vs demo.
 */

export type SignalDirection = "bullish" | "bearish" | "none";

/** The precomputed signal for a ticker under some active config (cfg-A/B/C). */
export interface SignalState {
  direction: SignalDirection;
  confidence: number | null; // 0..1
  config: string | null; // e.g. "cfg-C"
}

/** Sentiment + attention aggregates over the recent window. */
export interface AttentionState {
  sentiment: number; // mean finbert over in-window clusters, -1..1 (live, /screener/rows)
  heat: number; // signed sentiment x density: "loud + directional now" (cross-sectional)
  buzzZ: number | null; // latest daily buzz-z vs the ticker's own baseline (folded from /screener/rows)
  mentions: number; // distinct in-window attributed clusters — live
  authors: number; // distinct sources — live
}

export type EventKind = "FDA" | "M&A" | "SEC" | "EARN" | "NEWS" | null;

/** The most recent material catalyst for the ticker. */
export interface EventState {
  material: boolean;
  kind: EventKind;
  label: string | null; // "FDA · clin hold"
  ageHours: number | null;
}

/** Market data — no provider is wired in the project yet, so always demo. */
export interface PriceState {
  last: number | null;
  pctChange: number | null;
  volOverAvg: number | null;
}

/** Whether a field-group is backed by real data, news-derived, or placeholder. */
export type Origin = "live" | "derived" | "demo";

export interface Provenance {
  signal: Origin;
  attention: Origin;
  event: Origin;
  price: Origin;
}

/** Fundamentals overlay (from fundamentals_snapshots via /fundamentals) — brings
 * UNIVERSE-grade filters onto the news screener. Null where a ticker isn't in the
 * latest snapshot. */
export interface RowFundamentals {
  name: string | null; // Entity canonical name (drives the search combo's hint)
  sector: string | null;
  industry: string | null;
  marketCap: number | null; // $millions
  avgVolume: number | null; // thousands of shares
  shortFloat: number | null;
  beta: number | null;
}

export interface ScreenerRow {
  ticker: string;
  name: string;
  signal: SignalState;
  attention: AttentionState;
  event: EventState;
  price: PriceState;
  fundamentals: RowFundamentals | null;
  provenance: Provenance;
}

/** Active filter chips. Presentational + applied as predicates over rows. */
export interface ScreenerFilter {
  id: string;
  label: string;
  test: (row: ScreenerRow) => boolean;
}

export type Density = "compact" | "comfortable";
