import { NEWS_API } from "@/lib/config";
import { NewsItem } from "@/types/news";

const API_BASE = NEWS_API;

interface RawNewsItem extends Omit<NewsItem, "id"> {
  content_hash?: string;
  id?: string;
}

/**
 * Fetch recent ingested news items from the Screener middleware
 * (GET /api/news). Docs are already scored + ticker-tagged at ingestion time,
 * so the client just renders `sentiment` / `tickers` as-is — no client scoring.
 * Returns [] if the API is unreachable (middleware not running).
 */
export async function fetchNews(
  limit: number | null = 200,
  sourceType?: string,
): Promise<NewsItem[]> {
  try {
    const params = new URLSearchParams();
    if (limit !== null) params.set("limit", String(limit));
    if (sourceType) params.set("source_type", sourceType);
    const res = await fetch(`${API_BASE}/api/news?${params}`, { cache: "no-store" });
    if (!res.ok) return [];
    const data = await res.json();
    const items: RawNewsItem[] = data.items ?? [];
    // Map the Mongo content_hash to a stable `id` the UI keys on.
    return items.map((it) => ({
      ...it,
      id: it.id ?? it.content_hash ?? `${it.source}|${it.title}`,
    })) as NewsItem[];
  } catch {
    return [];
  }
}

/**
 * Recent news items for ONE ticker (GET /api/news?ticker=). Used to compute the
 * live intraday density curve (hourly buckets of published_at) on the ticker
 * chart — no new storage, straight from the news window.
 */
export async function fetchNewsForTicker(ticker: string, limit = 500): Promise<NewsItem[]> {
  try {
    const params = new URLSearchParams({ limit: String(limit), ticker });
    const res = await fetch(`${API_BASE}/api/news?${params}`, { cache: "no-store" });
    if (!res.ok) return [];
    const data = await res.json();
    const items: RawNewsItem[] = data.items ?? [];
    return items.map((it) => ({
      ...it,
      id: it.id ?? it.content_hash ?? `${it.source}|${it.title}`,
    })) as NewsItem[];
  } catch {
    return [];
  }
}

