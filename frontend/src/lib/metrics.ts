/**
 * Metrics client (PRED_API :8001, /metrics) — powers the EVAL surface. One row
 * per config_version (the signal config + each shadow baseline), so EVAL can show
 * the skill comparison the grader computes. All fields are real; null where a
 * quantity is undefined (e.g. hit_rate before anything resolves).
 */
import { PRED_API } from "@/lib/config";

export interface ConfigMetrics {
  config_version: string;
  total_graded: number;
  correct: number;
  incorrect: number;
  expired: number;
  hit_rate: number | null;
  coverage: number | null;
  precision: { bullish: number | null; bearish: number | null };
  recall: { bullish: number | null; bearish: number | null };
  mean_lead_time_days: number | null;
}

export interface MetricsResult {
  reachable: boolean;
  items: ConfigMetrics[];
}

export async function fetchMetrics(): Promise<MetricsResult> {
  try {
    const res = await fetch(`${PRED_API}/metrics`, { cache: "no-store" });
    if (!res.ok) return { reachable: false, items: [] };
    return { reachable: true, items: (await res.json()) as ConfigMetrics[] };
  } catch {
    return { reachable: false, items: [] };
  }
}
