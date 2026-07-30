/**
 * Client for the extended-session tracker (PRED_API :8001): per-day pre/regular/post
 * price behavior, accumulated. Movers = the tracked set's premarket movers for a day
 * (with a day-over-day streak); per-ticker history feeds the detail strip. Extended
 * fields are best-effort — thin names have no pre/after-hours prints, surfaced as null
 * ("--" in the UI), never fabricated.
 */
import { PRED_API } from "@/lib/config";

export interface PremarketStreak {
  direction: "gain" | "loss" | "flat" | null;
  count: number;
}

export interface ExtendedRow {
  ticker: string;
  date: string;
  prior_close: number | null;
  pm_last: number | null;
  pm_pct: number | null; // vs prior close (fraction)
  pm_high: number | null;
  pm_low: number | null;
  pm_volume: number | null;
  reg_open: number | null;
  reg_close: number | null;
  reg_pct: number | null; // vs prior close
  ah_last: number | null;
  ah_pct: number | null; // vs regular close
  ah_volume: number | null;
  source: string;
  /** present on movers rows */
  premarket_streak?: PremarketStreak;
}

export interface ExtendedDateOption {
  date: string; // ISO YYYY-MM-DD
  label: string; // e.g. "Fri Jul 24"
}

export interface ExtendedMovers {
  reachable: boolean;
  date: string;
  count: number;
  movers: ExtendedRow[];
  /** Sessions that actually have premarket movers (newest first) — the date selector. */
  available_dates: ExtendedDateOption[];
}

export interface ExtendedHistory {
  reachable: boolean;
  ticker: string;
  count: number;
  premarket_streak: PremarketStreak;
  rows: ExtendedRow[];
}

export async function fetchExtendedMovers(date?: string, limit = 50): Promise<ExtendedMovers> {
  const empty: ExtendedMovers = {
    reachable: false,
    date: date ?? "",
    count: 0,
    movers: [],
    available_dates: [],
  };
  try {
    const p = new URLSearchParams();
    if (date) p.set("date", date);
    p.set("limit", String(limit));
    const res = await fetch(`${PRED_API}/extended/movers?${p}`, { cache: "no-store" });
    if (!res.ok) return empty;
    return { reachable: true, ...(await res.json()) };
  } catch {
    return empty;
  }
}

export async function fetchTickerExtended(ticker: string, days = 30): Promise<ExtendedHistory> {
  const empty: ExtendedHistory = {
    reachable: false,
    ticker: ticker.toUpperCase(),
    count: 0,
    premarket_streak: { direction: null, count: 0 },
    rows: [],
  };
  try {
    const res = await fetch(
      `${PRED_API}/tickers/${encodeURIComponent(ticker)}/extended?days=${days}`,
      { cache: "no-store" },
    );
    if (!res.ok) return empty;
    return { reachable: true, ...(await res.json()) };
  } catch {
    return empty;
  }
}

/** "3rd straight premarket gain" style phrase, or null when there's no streak. */
export function streakPhrase(s: PremarketStreak | undefined): string | null {
  if (!s || !s.direction || s.count < 2) return null;
  const ord = (n: number): string => {
    const t = n % 100;
    if (t >= 11 && t <= 13) return `${n}th`;
    return `${n}${["th", "st", "nd", "rd"][Math.min(n % 10, 4)] ?? "th"}`;
  };
  const word = s.direction === "gain" ? "premarket gain" : s.direction === "loss" ? "premarket drop" : "flat premarket";
  return `${ord(s.count)} straight ${word}`;
}

/** Signed percent from a fraction, honest "--" for a non-number. */
export function pctStr(v: number | null | undefined): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "--";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;
}
