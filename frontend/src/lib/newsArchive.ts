/**
 * Client for the news ARCHIVE (PRED_API :8001): browse a past day's newsfeed from
 * OUR full-history raw_items plane, the same way the LIVE tape shows today. The live
 * tape reads the shallow Mongo window (/api/news); this reaches back day by day with
 * attribution (tickers/sentiment/catalyst via each item's origin cluster).
 *
 * Archive rows map onto the same NewsItem the live tape renders, so the LIVE page
 * swaps its source without touching the table. Member near-dupes carry no ticker/
 * sentiment overlay (their story's attribution lives on the origin) — honest nulls.
 */
import { PRED_API } from "@/lib/config";
import type { NewsItem, SentimentLabel } from "@/types/news";

export interface NewsDateOption {
  date: string; // ISO YYYY-MM-DD
  label: string; // e.g. "Wed Jul 16"
}

interface ArchiveItem {
  id: string;
  published_at: string;
  source: string | null;
  source_type: NewsItem["source_type"];
  title: string;
  url: string | null;
  tickers: string[];
  sentiment: { score: number } | null;
  catalyst_type: string | null;
  high_alert: boolean;
}

export interface NewsArchive {
  reachable: boolean;
  date: string;
  lane: string | null;
  count: number; // full filtered total for the day (for paging)
  limit: number;
  offset: number;
  items: NewsItem[];
}

export interface NewsArchiveQuery {
  date: string;
  lane?: "structured" | "social";
  ticker?: string;
  source?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

/** Label from the finbert score's sign — same tone convention the tape colors by. */
function labelOf(score: number): SentimentLabel {
  if (score > 0.05) return "bullish";
  if (score < -0.05) return "bearish";
  return "neutral";
}

/** Archive row -> NewsItem, so the LIVE tape's table renders it unchanged. */
function toNewsItem(a: ArchiveItem): NewsItem {
  return {
    id: a.id,
    source: a.source ?? "",
    source_type: a.source_type,
    title: a.title,
    published_at: a.published_at,
    description: "",
    url: a.url ?? "",
    topic: a.catalyst_type ?? "",
    tickers: a.tickers,
    sentiment: a.sentiment
      ? { score: a.sentiment.score, label: labelOf(a.sentiment.score), confidence: 1 }
      : undefined,
  };
}

/** Navigable dates (days that actually have news), newest first. */
export async function fetchNewsDates(): Promise<{ reachable: boolean; dates: NewsDateOption[] }> {
  try {
    const res = await fetch(`${PRED_API}/news/dates`, { cache: "no-store" });
    if (!res.ok) return { reachable: false, dates: [] };
    const data = await res.json();
    return { reachable: true, dates: data.dates ?? [] };
  } catch {
    return { reachable: false, dates: [] };
  }
}

/** One ET day's archived newsfeed, filtered + paginated (mirrors the tape filters). */
export async function fetchNewsArchive(query: NewsArchiveQuery): Promise<NewsArchive> {
  const { date, lane, ticker, source, q, limit = 1000, offset = 0 } = query;
  const empty: NewsArchive = {
    reachable: false,
    date,
    lane: lane ?? null,
    count: 0,
    limit,
    offset,
    items: [],
  };
  try {
    const p = new URLSearchParams({ date, limit: String(limit), offset: String(offset) });
    if (lane) p.set("lane", lane);
    if (ticker) p.set("ticker", ticker);
    if (source) p.set("source", source);
    if (q) p.set("q", q);
    const res = await fetch(`${PRED_API}/news/archive?${p}`, { cache: "no-store" });
    if (!res.ok) return empty;
    const data = await res.json();
    return {
      reachable: true,
      date: data.date,
      lane: data.lane,
      count: data.count,
      limit: data.limit,
      offset: data.offset,
      items: (data.items ?? []).map(toNewsItem),
    };
  } catch {
    return empty;
  }
}
