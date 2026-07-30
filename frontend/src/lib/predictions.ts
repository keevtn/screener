/**
 * Live prediction ledger client (PRED_API :8001, /predictions). Used to overlay
 * REAL signal state onto the screener instead of synthesizing it. Returns a
 * ticker -> most-recent-prediction map (the endpoint sorts by issued_at desc).
 */
import { PRED_API } from "@/lib/config";

export interface LivePrediction {
  ticker: string;
  direction: "bullish" | "bearish";
  confidence: number;
  config_version: string;
  status: string;
}

export async function fetchPredictionMap(limit = 300): Promise<Map<string, LivePrediction>> {
  const map = new Map<string, LivePrediction>();
  try {
    // kind=real: overlay the actual signal, never a baseline shadow (which shares a
    // real's ticker+issued_at, so it can otherwise win the first-per-ticker pick).
    const res = await fetch(`${PRED_API}/predictions?kind=real&limit=${limit}`, {
      cache: "no-store",
    });
    if (!res.ok) return map;
    const body = await res.json();
    for (const p of (body.items ?? []) as LivePrediction[]) {
      if (!map.has(p.ticker)) map.set(p.ticker, p); // first = most recent per ticker
    }
  } catch {
    /* prediction API down -> no live signal overlay */
  }
  return map;
}

/** Full ledger row (the LEDGER surface). Grader fills outcome/return after issue. */
export interface LedgerPrediction {
  prediction_id: string;
  ticker: string;
  direction: "bullish" | "bearish";
  confidence: number;
  horizon_trading_days: number;
  threshold: number;
  issued_at: string;
  config_version: string;
  status: string; // open | graded
  outcome: string | null; // correct | incorrect | expired
  realized_adjusted_return: number | null;
  graded_at: string | null; // set when the grader resolved it (drives the "newly graded" badge)
  // Origin-news context (companion table) — the LEDGER lane + article link.
  source_class: string | null; // "structured" | "social" | "mixed" | null (unresolved)
  headline: string | null; // originating article title
  url: string | null; // originating article link
  source: string | null; // originating source name
  // Baseline shadows (always_up/random/momentum) benchmark the real signal — same
  // ticker/issued_at/headline, own direction. LEDGER hides them by default.
  is_baseline: boolean;
  baseline_kind: string | null; // "always_up" | "random" | "momentum" when is_baseline
}

export interface LedgerResult {
  reachable: boolean;
  count: number;
  items: LedgerPrediction[];
}

export async function fetchLedger(
  opts: {
    status?: string;
    outcome?: string;
    sourceClass?: string;
    kind?: string;
    limit?: number;
  } = {},
): Promise<LedgerResult> {
  const p = new URLSearchParams();
  p.set("limit", String(opts.limit ?? 200));
  if (opts.status) p.set("status", opts.status);
  if (opts.outcome) p.set("outcome", opts.outcome);
  if (opts.sourceClass) p.set("source_class", opts.sourceClass);
  if (opts.kind) p.set("kind", opts.kind);
  try {
    const res = await fetch(`${PRED_API}/predictions?${p}`, { cache: "no-store" });
    if (!res.ok) return { reachable: false, count: 0, items: [] };
    const body = await res.json();
    return { reachable: true, count: body.count ?? 0, items: body.items ?? [] };
  } catch {
    return { reachable: false, count: 0, items: [] };
  }
}
