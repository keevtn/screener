/**
 * Live real-time quotes (Alpaca IEX via the prediction API's quote pump).
 *
 * Flow: a surface registers its visible tickers with watchLive() (TTL'd server
 * side, so re-register on an interval); the backend polls ONE batched
 * latest-trades request for the union and pushes `quotes` SSE events; surfaces
 * merge them over their polled prices. No Alpaca keys server-side -> watch
 * reports live:false and no events arrive — everything stays on polling.
 */

import { PRED_API } from "@/lib/config";

export interface LiveQuote {
  price: number;
  time: string;
}

export type QuoteMap = Record<string, LiveQuote>;

/** Register tickers for live pushes (fire-and-forget; re-call every ~60s). */
export async function watchLive(tickers: string[]): Promise<boolean> {
  const uniq = [...new Set(tickers.map((t) => t.toUpperCase()))].filter(Boolean);
  if (uniq.length === 0) return false;
  try {
    const res = await fetch(
      `${PRED_API}/live/watch?tickers=${encodeURIComponent(uniq.join(","))}`,
      { cache: "no-store" },
    );
    if (!res.ok) return false;
    return !!(await res.json()).live;
  } catch {
    return false;
  }
}
