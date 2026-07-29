/**
 * Per-ticker vs-own-history stats for the screener's INSIGHT strip
 * (GET /screener/stats — attention_daily rollup + buzz baselines). Ratios and
 * z-scores are null until a ticker has >= 5 observed days: the UI renders "—",
 * never a fabricated baseline.
 */

import { PRED_API } from "@/lib/config";

export interface TickerStats {
  n_days: number;
  avg_daily_mentions: number | null;
  mentions_today: number;
  struct_today: number;
  social_today: number;
  mentions_x_normal: number | null;
  sent_today: number | null;
  sent_hist_mean: number | null;
  sent_hist_std: number | null;
  sent_z: number | null;
  buzz_baseline: { mean: number; std: number; n_days: number; source: string } | null;
  /** Google-Trends daily search-interest, own-normalized (0-100). search_z is the
   * today-vs-own-history z (null until >=10 days); search_today is today's raw
   * index; search_days is the observed history depth. Hourly detail is separate
   * (on-demand /search-interest/hourly). */
  search_z: number | null;
  search_today: number | null;
  search_days: number;
}

export type StatsMap = Record<string, TickerStats>;

export async function fetchScreenerStats(tickers: string[]): Promise<StatsMap> {
  const uniq = [...new Set(tickers.map((t) => t.toUpperCase()))].filter(Boolean);
  if (uniq.length === 0) return {};
  try {
    const res = await fetch(
      `${PRED_API}/screener/stats?tickers=${encodeURIComponent(uniq.join(","))}`,
      { cache: "no-store" },
    );
    if (!res.ok) return {};
    return ((await res.json()).stats ?? {}) as StatsMap;
  } catch {
    return {};
  }
}
