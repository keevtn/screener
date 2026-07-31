export type SourceType = "rss" | "sec" | "fda" | "social";

export type SentimentLabel = "bullish" | "bearish" | "neutral";

export interface SentimentResult {
  score: number; // continuous [-1.0, 1.0]; negative = bearish
  label: SentimentLabel;
  confidence: number; // [0.0, 1.0]
}

/** Mirrors the Python NewsItem archived by storage_handlers.MongoHandler. */
export interface NewsItem {
  id: string; // mapped from content_hash by the API client
  source: string;
  source_type: SourceType;
  title: string;
  published_at: string; // ISO 8601 UTC
  description: string;
  url: string;
  topic: string; // comma-separated topic labels (TopicClassifier output)
  tickers?: string[];
  extra?: Record<string, unknown>;
  sentiment?: SentimentResult | null;
}

export type SortBy = "latest" | "score_desc" | "score_asc";

export interface FilterState {
  topics: Set<string>;
  /** Structured panel: which of rss/sec/fda to show. */
  sourceTypes: Set<SourceType>;
  /** Social panel: which platforms (Reddit/Bluesky/Other) to show. */
  platforms: Set<string>;
  sentiments: Set<SentimentLabel>;
  /** Feed labels (NewsItem.source) to include; empty set = all feeds. */
  sources: Set<string>;
  tickers: Set<string>;
  search: string;
  sortBy: SortBy;
  limit: number | null;
}
