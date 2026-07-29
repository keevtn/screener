/**
 * Client for the prediction API's catalyst-panel + preset endpoints (PRED_API,
 * :8001): fired catalysts, the scheduled calendar (earnings from Finviz), the
 * premarket morning panel (PMR), and the shared preset filters. These are the
 * same objects the screener and the Phase 7 ranker consume (I11) — one source,
 * rendered here as the Catalysts surface.
 */
import { PRED_API } from "@/lib/config";

export interface ScheduledEvent {
  ticker: string;
  catalyst_type: string;
  event_date: string;
  days_until: number | null;
  stage: string | null;
  source: string;
  meta: Record<string, unknown>;
}

export interface FiredCatalyst {
  cluster_id: string;
  catalyst_type: string;
  event_stage: string | null;
  materiality: number;
  high_alert: boolean;
  /** Two-axis sentiment (I7: kept separate). finbert_score is the primary tone
   * the row's tape-style badge renders; lm_score is the secondary axis (tooltip);
   * finbert_label is the tag. Any may be null on unscored/legacy rows. */
  finbert_score: number | null;
  lm_score: number | null;
  finbert_label: string | null;
  published_at: string;
  /** When the system first scored/classified the cluster — the call time.
   * Null only on rows persisted before call-time tracking. */
  called_at: string | null;
  title: string | null;
  /** Origin item's source URL, so the fired row can link through to the article. */
  url: string | null;
  tickers: { ticker: string; role: string }[];
  rank: number;
}

export interface PanelData {
  reachable: boolean;
  scheduled: ScheduledEvent[];
  fired: FiredCatalyst[];
  presets: string[];
}

/** One ranked ticker on the PMR morning panel (frozen at 09:25 ET, or a live
 * recompute during the premarket window). Mirrors pipeline.panel.premarket's
 * PremarketRow dataclass; the cluster_* fields are the ticker's top catalyst. */
export interface PremarketRow {
  ticker: string;
  score: number;
  lean: "long" | "short" | "mixed" | "none";
  n_clusters: number;
  /** No overnight news — on the panel only for today's scheduled earnings. */
  scheduled_only: boolean;
  /** Earnings scheduled for this session (approximate — not a confirmed BMO). */
  earnings_today: boolean;
  finbert_score: number | null;
  buzz_z: number | null;
  cluster_id: string | null;
  title: string | null;
  catalyst_type: string | null;
  event_stage: string | null;
  materiality: number | null;
  high_alert: boolean;
  published_at: string | null;
  /** Age of the top catalyst at panel compute time (frozen, not live). */
  age_hours: number | null;
}

/** Next-close grading for one panel ticker (present once the panel is graded). */
export interface PremarketOutcome {
  gap_return: number | null;
  oc_return: number;
  lean_hit: boolean | null;
}

export interface PremarketSummary {
  graded_n: number;
  total: number;
  top5_mean_abs_oc: number | null;
  rest_mean_abs_oc: number | null;
  lean_hit_rate: number | null;
}

export interface PremarketData {
  reachable: boolean;
  available: boolean;
  live: boolean;
  session_date?: string;
  computed_at?: string;
  window?: { start: string; end: string };
  /** Not this session's panel (weekend/holiday serves the prior session). */
  stale?: boolean;
  count: number;
  rows: PremarketRow[];
  graded?: boolean;
  outcomes?: Record<string, PremarketOutcome> | null;
  summary?: PremarketSummary | null;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${PRED_API}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return (await res.json()) as T;
}

export async function fetchScheduled(limit = 100): Promise<ScheduledEvent[]> {
  const b = await getJSON<{ items: ScheduledEvent[] }>(`/catalysts/scheduled?limit=${limit}`);
  return b.items;
}

export async function fetchFired(
  limit = 50,
  windowDays = 7,
  order: "recent" | "rank" = "rank",
): Promise<FiredCatalyst[]> {
  const b = await getJSON<{ items: FiredCatalyst[] }>(
    `/catalysts/fired?limit=${limit}&window_days=${windowDays}&order=${order}`,
  );
  return b.items;
}

/** PMR morning panel: today's frozen snapshot by default, ?live=1 for a live
 * recompute while the premarket window is open (the API falls back to stored
 * outside it). Degrades to reachable:false when :8001 is down, mirroring
 * loadPanelData — the band polls this on its own cadence. */
export async function fetchPremarket(live = false): Promise<PremarketData> {
  try {
    const b = await getJSON<Omit<PremarketData, "reachable">>(
      `/catalysts/premarket${live ? "?live=1" : ""}`,
    );
    return { reachable: true, ...b };
  } catch {
    return { reachable: false, available: false, live: false, count: 0, rows: [] };
  }
}

export async function fetchPresetNames(): Promise<string[]> {
  const b = await getJSON<{ presets: Record<string, unknown> }>("/presets");
  return Object.keys(b.presets ?? {});
}

/** Load the whole Catalysts surface in one shot; degrades gracefully if :8001 is down.
 * The FIRED feed spans `firedWindowDays` (default 1 week), materiality-ranked with a
 * window-scaled half-life (so week-old majors surface, not just the last few hours at
 * high volume), up to `firedLimit` rows (the panel scrolls internally). */
export async function loadPanelData(
  firedWindowDays = 7,
  firedLimit = 200,
): Promise<PanelData> {
  try {
    const [scheduled, fired, presets] = await Promise.all([
      fetchScheduled(100),
      fetchFired(firedLimit, firedWindowDays, "rank"),
      fetchPresetNames(),
    ]);
    return { reachable: true, scheduled, fired, presets };
  } catch {
    return { reachable: false, scheduled: [], fired: [], presets: [] };
  }
}
