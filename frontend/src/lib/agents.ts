/**
 * Client for the prediction API's Phase 7 agent endpoints (default port 8001,
 * separate from the news middleware on 8000). Powers the AI-ranking surface and
 * its force-run control: pick a model + timeframe, POST /agents/rank/run, render
 * the cited watchlist. The ranker only PROPOSES — nothing here changes config or
 * the ledger.
 */

import { PRED_API } from "@/lib/config";

export interface RankingItem {
  rank: number;
  ticker: string;
  direction: "bullish" | "bearish" | "neutral";
  conviction: number;
  rationale: string;
  evidence_ids: string[];
}

export interface RankingRun {
  run_id: string;
  created_at: string;
  trigger: string;
  model: string;
  horizon_trading_days: number;
  candidate_count: number;
  status: "ok" | "empty" | "failed";
  config_version: string | null;
  error: string | null;
  items: RankingItem[];
}

export interface Spend {
  today_usd: number;
  total_usd: number;
  calls: number;
  /** Daily soft cap in USD (AGENT_DAILY_USD_CAP, default $2). */
  cap_usd: number;
  /** today_usd / cap_usd. */
  pct_of_cap: number;
}

export interface ModelList {
  models: string[];
  default: string;
}

/** A cited cluster resolved to a real headline + scores (the RANK evidence pulldown). */
export interface EvidenceCluster {
  cluster_id: string;
  title: string | null;
  source: string | null;
  source_class: string | null;
  url: string | null;
  published_at: string;
  catalyst_type: string | null;
  event_stage: string | null;
  finbert_score: number | null;
  materiality: number | null;
  high_alert: boolean;
  tickers: string[];
}

export interface ForceRunRequest {
  model: string;
  horizon_trading_days?: number | null;
  presets?: string[];
  high_alert?: boolean;
  extreme_sentiment?: boolean;
  limit?: number;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${PRED_API}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return (await res.json()) as T;
}

export async function fetchModels(): Promise<ModelList> {
  return getJSON<ModelList>("/agents/models");
}

export async function fetchRankings(limit = 20): Promise<RankingRun[]> {
  return getJSON<RankingRun[]>(`/agents/rankings?limit=${limit}`);
}

export async function fetchSpend(): Promise<Spend> {
  return getJSON<Spend>("/agents/spend");
}

/** Resolve a ranking's cited cluster_ids to their headlines/scores (evidence pulldown). */
export async function fetchEvidence(ids: string[]): Promise<EvidenceCluster[]> {
  if (ids.length === 0) return [];
  const q = encodeURIComponent(ids.join(","));
  const r = await getJSON<{ items: EvidenceCluster[] }>(`/clusters/resolve?ids=${q}`);
  return r.items;
}

/** Force-run a ranking. Throws with the API detail on 4xx/5xx (e.g. 429 soft cap). */
export async function runRanking(body: ForceRunRequest): Promise<RankingRun> {
  const res = await fetch(`${PRED_API}/agents/rank/run`, {
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
    throw new Error(detail);
  }
  return (await res.json()) as RankingRun;
}

/** Human-friendly model label for the dropdown. */
export function modelLabel(id: string): string {
  if (id.startsWith("claude-opus")) return "Opus 4.8";
  if (id.startsWith("claude-sonnet")) return "Sonnet 5";
  if (id.startsWith("claude-haiku")) return "Haiku 4.5";
  return id;
}
