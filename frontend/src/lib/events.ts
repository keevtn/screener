/**
 * Real-time event stream client (SSE). Two streams, one per backend:
 *   - NEWS_EVENTS (:8000 /api/events)  — new news items (the middleware's Mongo watcher)
 *   - PIPE_EVENTS (:8001 /events)      — fired catalysts, predictions, grades, rankings
 *
 * Push is an ACCELERATOR, never a dependency: every surface keeps its polling
 * cadence, and an event just triggers the same refresh immediately. If the
 * stream drops, EventSource auto-reconnects with backoff and polling carries on
 * — degrade-graceful by construction.
 */

import { NEWS_API, PRED_API } from "@/lib/config";

export interface TapeEvent {
  type: string; // news | fired | predictions | grades | ranking | deep_dive
  at: string;
  count?: number;
  [k: string]: unknown;
}

export const NEWS_EVENTS = `${NEWS_API}/api/events`;
export const PIPE_EVENTS = `${PRED_API}/events`;

/**
 * Subscribe to an SSE stream. Returns an unsubscribe/cleanup function.
 * `types` filters which event types invoke the callback (empty = all).
 */
export function subscribeEvents(
  url: string,
  onEvent: (e: TapeEvent) => void,
  types: string[] = [],
): () => void {
  if (typeof window === "undefined" || typeof EventSource === "undefined") {
    return () => {};
  }
  let es: EventSource | null = null;
  let closed = false;
  let retry: ReturnType<typeof setTimeout> | null = null;

  function open() {
    if (closed) return;
    es = new EventSource(url);
    es.onmessage = (m) => {
      try {
        const e = JSON.parse(m.data) as TapeEvent;
        if (types.length === 0 || types.includes(e.type)) onEvent(e);
      } catch {
        /* ignore malformed frames */
      }
    };
    // EventSource retries transient errors itself; if the browser gives up
    // (readyState CLOSED) re-open after a delay so long outages still recover.
    es.onerror = () => {
      if (es && es.readyState === EventSource.CLOSED && !closed) {
        es.close();
        retry = setTimeout(open, 10_000);
      }
    };
  }
  open();
  return () => {
    closed = true;
    if (retry) clearTimeout(retry); // don't leave a reconnect timer after unmount
    es?.close();
  };
}
