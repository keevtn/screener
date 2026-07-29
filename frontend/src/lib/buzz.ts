/**
 * Buzz + heat helpers for the screener.
 *
 * buzz-z (from /buzz/latest) = current density vs the ticker's OWN baseline
 * (intra-ticker anomaly, sentiment-blind). heat() = signed cross-sectional
 * "loud and directional now": mean_sentiment * shrink(mentions) * log1p(mentions),
 * where the shrink term damps single-article tickers so noise can't top the board.
 */
import { PRED_API } from "@/lib/config";

export async function fetchBuzzLatest(): Promise<Map<string, number>> {
  const map = new Map<string, number>();
  try {
    const res = await fetch(`${PRED_API}/buzz/latest`, { cache: "no-store" });
    if (!res.ok) return map;
    const body = await res.json();
    for (const [t, z] of Object.entries((body.buzz ?? {}) as Record<string, number>)) {
      map.set(t, z);
    }
  } catch {
    /* prediction API down -> no buzz overlay */
  }
  return map;
}

const HEAT_K = 3; // low-mention shrink strength (shared by heat + attnSentiment)

export function heat(sentiment: number, mentions: number): number {
  if (mentions <= 0) return 0;
  return sentiment * (mentions / (mentions + HEAT_K)) * Math.log1p(mentions);
}

/**
 * attnSentiment — heat()'s BOUNDED charting sibling: conviction-damped tone in
 * [−1, +1]. Same shrink K as heat() (shared constant so the two never drift
 * apart silently) but NO log1p volume term — on a chart the mentions histogram
 * already carries intensity; this answers "how much should I trust the tone?".
 * One lukewarm post shrinks toward 0; a 30-item hour approaches the raw mean.
 */
export function attnSentiment(meanSent: number, n: number, k: number = HEAT_K): number {
  if (n <= 0) return 0;
  return meanSent * (n / (n + k));
}
