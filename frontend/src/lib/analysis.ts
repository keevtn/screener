/**
 * Client for the prediction API's DEEP DIVE endpoints (single-ticker AI analysis
 * over our OWN data — no internet). GET is instant (reads the persisted analysis);
 * POST runs a fresh model call, rate limited to a few distinct tickers per rolling
 * window (429 + Retry-After). The analysis PROPOSES a view; it never trades or edits
 * config.
 */

import { PRED_API } from "@/lib/config";

export interface DeepDiveEvidence {
  point: string;
  cluster_id: string | null;
}

/** One recent scored cluster in the assembled evidence snapshot (for provenance). */
export interface EvidenceCluster {
  cluster_id: string;
  published_at: string;
  source: string;
  title: string | null;
  description: string | null;
  catalyst_type: string | null;
  materiality: number | null;
  finbert_score: number | null;
  lm_score: number | null;
}

export interface AnalysisEvidence {
  ticker: string;
  window: {
    sentiment_composite: number | null;
    materiality_composite: number | null;
    item_count: number;
  };
  clusters: EvidenceCluster[];
  next_earnings: string | null;
  fundamentals: Record<string, unknown> | null;
  predictions: unknown[];
}

export interface TickerAnalysis {
  analysis_id: string;
  ticker: string;
  created_at: string;
  model: string;
  horizon_trading_days: number;
  config_version: string | null;
  status: "ok" | "failed" | "empty";
  direction: "bullish" | "bearish" | "neutral" | null;
  conviction: number | null;
  thesis: string | null;
  key_evidence: DeepDiveEvidence[];
  risks: string[];
  what_would_change_my_mind: string[];
  evidence: AnalysisEvidence | Record<string, never>;
  error: string | null;
}

/** Thrown on a 429 so the UI can show a countdown. */
export class RateLimitError extends Error {
  retryAfter: number;
  constructor(message: string, retryAfter: number) {
    super(message);
    this.name = "RateLimitError";
    this.retryAfter = retryAfter;
  }
}

/** Latest persisted deep dive for a ticker, or null if never analyzed. */
export async function fetchAnalysis(ticker: string): Promise<TickerAnalysis | null> {
  const res = await fetch(`${PRED_API}/tickers/${encodeURIComponent(ticker)}/analysis`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`GET analysis -> ${res.status}`);
  const j = await res.json();
  return j as TickerAnalysis | null;
}

export interface RunDeepDiveRequest {
  model: string;
  horizon_trading_days?: number | null;
}

/** Run a fresh deep dive. Throws RateLimitError on 429, Error otherwise. */
export async function runDeepDive(
  ticker: string,
  body: RunDeepDiveRequest
): Promise<TickerAnalysis> {
  const res = await fetch(`${PRED_API}/tickers/${encodeURIComponent(ticker)}/analyze`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch {
      /* keep the status-code detail */
    }
    if (res.status === 429) {
      const ra = Number(res.headers.get("retry-after") ?? "0") || 0;
      throw new RateLimitError(detail, ra);
    }
    throw new Error(detail);
  }
  return (await res.json()) as TickerAnalysis;
}
