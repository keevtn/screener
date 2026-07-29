/**
 * Price client (PRED_API :8001, /marketdata/prices) — cache-only latest quotes
 * from the parquet bar cache (no network fetch server-side). Uncached tickers are
 * absent from the map, so the UI renders them as "—" (never fabricated).
 */
import { PRED_API } from "@/lib/config";

export interface Quote {
  ticker: string;
  last: number;
  pct_change: number | null;
  vol_over_avg: number | null;
  as_of: string;
}

export async function fetchPrices(tickers: string[]): Promise<Map<string, Quote>> {
  const map = new Map<string, Quote>();
  const uniq = [...new Set(tickers.map((t) => t.toUpperCase()))].filter(Boolean);
  if (uniq.length === 0) return map;
  try {
    const res = await fetch(`${PRED_API}/marketdata/prices?tickers=${uniq.join(",")}`, {
      cache: "no-store",
    });
    if (!res.ok) return map;
    const body = await res.json();
    for (const [t, q] of Object.entries((body.prices ?? {}) as Record<string, Quote>)) {
      map.set(t, q);
    }
  } catch {
    /* prediction API down -> no prices, UI shows "—" */
  }
  return map;
}
