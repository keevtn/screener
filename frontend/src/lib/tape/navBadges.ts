/**
 * "New since last looked" nav badges (replaces the retired ALERTS panel — the
 * decision being that CATALYSTS already gives full alert coverage, so alerting
 * belongs as subtle per-surface counters rather than its own screen).
 *
 * Per-surface last-seen timestamps live in localStorage; each surface stamps
 * itself "seen" on view (markSeen), and TapeNav shows how many items have landed
 * since. Everything is client-side and best-effort: if a backend is down the
 * badge is simply absent. No new persistence, no server state.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchFired, type FiredCatalyst } from "@/lib/catalysts";
import { fetchLedger, type LedgerPrediction } from "@/lib/predictions";
import { fetchNews } from "@/lib/api";
import { NEWS_EVENTS, PIPE_EVENTS, subscribeEvents } from "@/lib/events";
import type { NewsItem } from "@/types/news";

export type BadgeSurface = "LIVE" | "CATALYSTS" | "LEDGER";

export interface NavBadges {
  LIVE: { count: number };
  CATALYSTS: { count: number; high: number };
  LEDGER: { count: number; graded: number };
}

const EMPTY: NavBadges = {
  LIVE: { count: 0 },
  CATALYSTS: { count: 0, high: 0 },
  LEDGER: { count: 0, graded: 0 },
};

const KEY = (s: string) => `tape:lastSeen:${s}`;
const SEEN_EVENT = "tape:seen";

/** ms-epoch of the last time `surface` was viewed, or null if never. */
export function getLastSeen(surface: BadgeSurface): number | null {
  if (typeof window === "undefined") return null;
  const v = window.localStorage.getItem(KEY(surface));
  return v == null ? null : Number(v);
}

/** Stamp a surface as seen "now" and notify any mounted nav to recompute. */
export function markSeen(surface: BadgeSurface, at: number = Date.now()): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(KEY(surface), String(at));
  window.dispatchEvent(new CustomEvent(SEEN_EVENT, { detail: { surface } }));
}

/** Pure: count items whose timestamp (ISO string) is strictly after `since`. */
export function countNewer<T>(
  items: T[],
  ts: (i: T) => string | null | undefined,
  since: number,
): number {
  let n = 0;
  for (const it of items) {
    const t = ts(it);
    if (t) {
      const ms = Date.parse(t);
      if (!Number.isNaN(ms) && ms > since) n += 1;
    }
  }
  return n;
}

interface RawData {
  fired: FiredCatalyst[];
  ledger: LedgerPrediction[];
  news: NewsItem[];
}

/**
 * On first ever sight of a surface we initialize its last-seen to "now" so the
 * user isn't greeted by a badge counting the entire backlog. Returns the
 * effective baseline (now for a fresh surface, the stored value otherwise).
 */
function baseline(surface: BadgeSurface): number {
  const seen = getLastSeen(surface);
  if (seen == null) {
    const now = Date.now();
    if (typeof window !== "undefined") window.localStorage.setItem(KEY(surface), String(now));
    return now;
  }
  return seen;
}

/**
 * Nav badge counts for the three activity surfaces. Polls lightly (every 45s)
 * and recomputes instantly when any surface is marked seen (this or another tab).
 * Best-effort: a down backend just yields zeros for that surface.
 */
export function useNavBadges(): NavBadges {
  const [badges, setBadges] = useState<NavBadges>(EMPTY);
  const dataRef = useRef<RawData>({ fired: [], ledger: [], news: [] });

  const recompute = useCallback(() => {
    const { fired, ledger, news } = dataRef.current;
    const cat = baseline("CATALYSTS");
    const led = baseline("LEDGER");
    const live = baseline("LIVE");
    setBadges({
      LIVE: { count: countNewer(news, (i) => i.published_at, live) },
      CATALYSTS: {
        count: countNewer(fired, (i) => i.published_at, cat),
        high: countNewer(
          fired.filter((f) => f.high_alert),
          (i) => i.published_at,
          cat,
        ),
      },
      LEDGER: {
        count: countNewer(ledger, (i) => i.issued_at, led),
        graded: countNewer(ledger, (i) => i.graded_at, led),
      },
    });
  }, []);

  const refetch = useCallback(async () => {
    const [fired, ledger, news] = await Promise.all([
      fetchFired(50).catch(() => [] as FiredCatalyst[]),
      fetchLedger({ limit: 300 })
        .then((r) => r.items)
        .catch(() => [] as LedgerPrediction[]),
      fetchNews(300).catch(() => [] as NewsItem[]),
    ]);
    dataRef.current = { fired, ledger, news };
    recompute();
  }, [recompute]);

  useEffect(() => {
    refetch();
    const poll = setInterval(refetch, 45_000);
    // Real-time push: refetch the instant a backend announces new data (SSE).
    // Polling above stays as the fallback when the streams are down.
    const unNews = subscribeEvents(NEWS_EVENTS, () => refetch(), ["news"]);
    const unPipe = subscribeEvents(PIPE_EVENTS, () => refetch(), [
      "news",
      "fired",
      "predictions",
      "grades",
    ]);
    // Recompute (no refetch) the moment a surface is marked seen, or another tab
    // updates localStorage.
    window.addEventListener(SEEN_EVENT, recompute);
    window.addEventListener("storage", recompute);
    return () => {
      clearInterval(poll);
      unNews();
      unPipe();
      window.removeEventListener(SEEN_EVENT, recompute);
      window.removeEventListener("storage", recompute);
    };
  }, [refetch, recompute]);

  return badges;
}
