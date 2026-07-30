/**
 * Universe screener client (PRED_API :8001) — the Finviz-style surface. Filters
 * run server-side over the daily fundamentals_snapshots (Finviz Elite export),
 * with our own signal + next-earnings overlaid. All fields real; market cap in
 * $millions, avg volume in thousands, ratios as fractions (0.03 = 3%).
 */
import { PRED_API } from "@/lib/config";

export interface UniverseRow {
  ticker: string;
  name: string | null;
  sector: string | null;
  industry: string | null;
  market_cap: number | null;
  price: number | null;
  change_pct: number | null;
  avg_volume: number | null;
  short_float: number | null;
  inst_own: number | null;
  insider_own: number | null;
  beta: number | null;
  signal: { direction: string; confidence: number } | null;
  next_earnings: string | null;
}

export interface UniverseFacets {
  as_of: string | null;
  universe: number;
  sectors: { name: string; count: number }[];
  industries: string[];
}

export interface UniverseResult {
  reachable: boolean;
  count: number;
  limit: number;
  offset: number;
  items: UniverseRow[];
}

export interface UniverseFilters {
  /** Ticker-prefix OR company-name-substring search (server-side, whole universe). */
  q?: string;
  sector?: string;
  industry?: string;
  mcap_min?: number;
  mcap_max?: number;
  price_min?: number;
  price_max?: number;
  avgvol_min?: number;
  short_min?: number;
  short_max?: number;
  inst_min?: number;
  insider_min?: number;
  beta_min?: number;
  beta_max?: number;
  change_min?: number;
  change_max?: number;
  has_signal?: boolean;
  earnings_within?: number;
  sort?: string;
  order?: string;
  limit?: number;
  offset?: number;
}

export interface Fundamentals {
  name: string | null;
  sector: string | null;
  industry: string | null;
  market_cap: number | null; // $millions
  avg_volume: number | null; // thousands of shares
  short_float: number | null;
  beta: number | null;
}

/** Bulk fundamentals overlay for a ticker list (the screener's mcap/sector/vol). */
export async function fetchFundamentals(tickers: string[]): Promise<Map<string, Fundamentals>> {
  const map = new Map<string, Fundamentals>();
  const uniq = [...new Set(tickers.map((t) => t.toUpperCase()))].filter(Boolean);
  if (uniq.length === 0) return map;
  try {
    const res = await fetch(`${PRED_API}/fundamentals?tickers=${uniq.join(",")}`, {
      cache: "no-store",
    });
    if (!res.ok) return map;
    const body = await res.json();
    for (const [t, f] of Object.entries((body.fundamentals ?? {}) as Record<string, Fundamentals>)) {
      map.set(t, f);
    }
  } catch {
    /* prediction API down -> no fundamentals overlay */
  }
  return map;
}

export async function fetchUniverseFacets(): Promise<UniverseFacets | null> {
  try {
    const res = await fetch(`${PRED_API}/universe/facets`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as UniverseFacets;
  } catch {
    return null;
  }
}

export async function fetchUniverseScreen(f: UniverseFilters): Promise<UniverseResult> {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  try {
    const res = await fetch(`${PRED_API}/universe/screen?${p}`, { cache: "no-store" });
    if (!res.ok) return { reachable: false, count: 0, limit: 50, offset: 0, items: [] };
    const b = await res.json();
    return { reachable: true, ...b };
  } catch {
    return { reachable: false, count: 0, limit: 50, offset: 0, items: [] };
  }
}

// --- display formatters (Finviz units) --------------------------------------
// Guard with Number.isFinite (not just `== null`): a REAL DB column can hand back
// a non-numeric string (SQLite is dynamically typed), and `"ts".toFixed()` throws.
// A non-number renders as an honest "—" rather than crashing the row/panel.
export function fmtMcap(m: number | null): string {
  if (!Number.isFinite(m as number)) return "—";
  const n = m as number;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}T`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}B`;
  return `$${n.toFixed(0)}M`;
}

export function fmtVol(v: number | null): string {
  if (!Number.isFinite(v as number)) return "—";
  const n = v as number;
  return n >= 1000 ? `${(n / 1000).toFixed(1)}M` : `${n.toFixed(0)}K`;
}

export function fmtPct(v: number | null): string {
  return Number.isFinite(v as number) ? `${((v as number) * 100).toFixed(1)}%` : "—";
}

// Finviz market-cap buckets (values in $millions).
export const MCAP_BUCKETS: { label: string; min?: number; max?: number }[] = [
  { label: "Any" },
  { label: "Mega ($200B+)", min: 200_000 },
  { label: "Large ($10–200B)", min: 10_000, max: 200_000 },
  { label: "Mid ($2–10B)", min: 2_000, max: 10_000 },
  { label: "Small ($300M–2B)", min: 300, max: 2_000 },
  { label: "Micro ($50–300M)", min: 50, max: 300 },
  { label: "Nano (<$50M)", max: 50 },
];
