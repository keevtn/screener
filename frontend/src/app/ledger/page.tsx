"use client";

import { useEffect, useMemo, useState } from "react";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import HealthStrip from "@/components/tape/HealthStrip";
import { TickerButton } from "@/components/tape/TickerModalProvider";
import { fetchLedger, type LedgerPrediction } from "@/lib/predictions";
import { markSeen } from "@/lib/tape/navBadges";
import { PIPE_EVENTS, subscribeEvents } from "@/lib/events";

/**
 * TAPE_ LEDGER — the immutable prediction ledger (docs/ROADMAP.md I3/I4).
 *
 * Every issued prediction, with the grader-filled outcome fields. Read-only, all
 * real from /predictions (:8001). status/outcome/return are exactly what the
 * grader wrote — nothing synthesized. Each row also carries its originating-news
 * context (the STRUCTURED vs SOCIAL lane + the article headline/link) from the
 * companion prediction_context table, served on the same endpoint (no N+1).
 */

const STATUSES = ["all", "open", "graded"] as const;
type StatusFilter = (typeof STATUSES)[number];

const LANES = ["all", "structured", "social"] as const;
type LaneFilter = (typeof LANES)[number];

// One fetch pulls the most-recent slice; lanes split client-side so switching is
// instant and per-lane counts are visible. Rendering is paged to stay snappy.
const FETCH_LIMIT = 1000;
const PAGE_SIZE = 50;

function dirColor(d: string): string {
  return d === "bullish" ? "text-tape-bull" : d === "bearish" ? "text-tape-bear" : "text-tape-muted";
}

function outcomeColor(o: string | null): string {
  if (o === "correct") return "text-tape-bull";
  if (o === "incorrect") return "text-tape-bear";
  if (o === "expired") return "text-tape-warn";
  return "text-tape-faint";
}

function fmtDate(iso: string): string {
  return iso.slice(0, 16).replace("T", " ");
}

function isHttp(url: string | null): url is string {
  return !!url && (url.startsWith("http://") || url.startsWith("https://"));
}

function laneOf(p: LedgerPrediction): LaneFilter | "unknown" {
  if (p.source_class === "structured") return "structured";
  if (p.source_class === "social") return "social";
  return "unknown"; // mixed/null — only ever shown under the ALL lane
}

export default function LedgerPage() {
  const clock = useClock();
  const [status, setStatus] = useState<StatusFilter>("all");
  const [lane, setLane] = useState<LaneFilter>("all");
  const [page, setPage] = useState(0);
  const [items, setItems] = useState<LedgerPrediction[]>([]);
  const [reachable, setReachable] = useState(true);
  const [loading, setLoading] = useState(true);
  // Bumped by SSE on new predictions / grades -> instant refresh; poll = fallback.
  const [pushTick, setPushTick] = useState(0);

  useEffect(
    () =>
      subscribeEvents(PIPE_EVENTS, () => setPushTick((n) => n + 1), ["predictions", "grades"]),
    [],
  );

  useEffect(() => {
    setLoading(true);
  }, [status]);

  // Reset to the first page whenever the visible set changes.
  useEffect(() => {
    setPage(0);
  }, [status, lane]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      // Lane is applied client-side (below) so the fetched window carries every
      // lane at once — instant switching + honest per-lane counts.
      const r = await fetchLedger({
        status: status === "all" ? undefined : status,
        limit: FETCH_LIMIT,
      });
      if (cancelled) return;
      setItems(r.items);
      setReachable(r.reachable);
      setLoading(false);
      markSeen("LEDGER"); // viewing the ledger clears new-prediction/newly-graded badges
    }
    load();
    const t = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [status, pushTick]);

  const laneCounts = useMemo(() => {
    const c = { all: items.length, structured: 0, social: 0 };
    for (const p of items) {
      const l = laneOf(p);
      if (l === "structured") c.structured += 1;
      else if (l === "social") c.social += 1;
    }
    return c;
  }, [items]);

  const laneItems = useMemo(() => {
    if (lane === "all") return items;
    return items.filter((p) => laneOf(p) === lane);
  }, [items, lane]);

  const stats = useMemo(() => {
    const graded = laneItems.filter((p) => p.status === "graded");
    const resolved = graded.filter((p) => p.outcome === "correct" || p.outcome === "incorrect");
    const correct = graded.filter((p) => p.outcome === "correct").length;
    return {
      total: laneItems.length,
      open: laneItems.filter((p) => p.status === "open").length,
      graded: graded.length,
      hitRate: resolved.length ? correct / resolved.length : null,
    };
  }, [laneItems]);

  const pageCount = Math.max(1, Math.ceil(laneItems.length / PAGE_SIZE));
  const clampedPage = Math.min(page, pageCount - 1);
  const pageItems = useMemo(
    () => laneItems.slice(clampedPage * PAGE_SIZE, clampedPage * PAGE_SIZE + PAGE_SIZE),
    [laneItems, clampedPage],
  );

  return (
    <>
      <TapeNav active="LEDGER" clock={clock} />

      <div className="shrink-0 flex flex-wrap items-center gap-x-5 gap-y-2 px-[22px] py-3 border-b border-tape-border bg-tape-panel-2 tape-mono text-[11px]">
        <span className="text-tape-muted tracking-[0.12em] font-semibold">LEDGER</span>
        <div className="flex items-center gap-1">
          {STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => setStatus(s)}
              className={`px-2.5 py-1 rounded tracking-[0.08em] uppercase ${
                status === s
                  ? "bg-tape-panel text-tape-accent border border-tape-border"
                  : "text-tape-faint hover:text-tape-sub border border-transparent"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
        <span className="text-tape-border-soft">·</span>
        <div className="flex items-center gap-1">
          {LANES.map((l) => (
            <button
              key={l}
              onClick={() => setLane(l)}
              className={`px-2.5 py-1 rounded tracking-[0.08em] uppercase flex items-center gap-1.5 ${
                lane === l
                  ? "bg-tape-panel text-tape-accent border border-tape-border"
                  : "text-tape-faint hover:text-tape-sub border border-transparent"
              }`}
              title={
                l === "social"
                  ? "predictions whose originating news is social (I8: the signal engine is structured-only, so this lane is typically empty)"
                  : undefined
              }
            >
              {l}
              <span className="text-tape-dim">{laneCounts[l]}</span>
            </button>
          ))}
        </div>
        <span className="text-tape-faint ml-auto flex gap-4">
          <span>
            total <span className="text-tape-sub">{stats.total}</span>
          </span>
          <span>
            open <span className="text-tape-sub">{stats.open}</span>
          </span>
          <span>
            graded <span className="text-tape-sub">{stats.graded}</span>
          </span>
          <span>
            hit rate{" "}
            <span className="text-tape-text">
              {stats.hitRate == null ? "—" : `${(stats.hitRate * 100).toFixed(0)}%`}
            </span>
          </span>
        </span>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted">
          loading ledger…
        </div>
      ) : !reachable ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          Prediction API not reachable on :8001 — start it with scripts/serve_api.py.
        </div>
      ) : laneItems.length === 0 ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          {lane === "social"
            ? "No social-origin predictions — the signal engine is structured-only (I8), so this lane is empty."
            : "No predictions in the ledger for this filter."}
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
          <table className="w-full border-collapse tape-mono text-[11px]">
            <thead>
              <tr className="sticky top-0 bg-tape-panel-2 text-tape-muted text-left tracking-[0.1em] border-b border-tape-border z-10">
                <th className="px-4 py-2 font-semibold w-20">TICKER</th>
                <th className="px-2 py-2 font-semibold w-20">DIR</th>
                <th className="px-2 py-2 font-semibold w-16">CONF</th>
                <th className="px-2 py-2 font-semibold w-14">HZN</th>
                <th className="px-3 py-2 font-semibold w-36">ISSUED</th>
                <th className="px-3 py-2 font-semibold">ORIGIN NEWS</th>
                <th className="px-2 py-2 font-semibold w-20">STATUS</th>
                <th className="px-2 py-2 font-semibold w-24">OUTCOME</th>
                <th className="px-2 py-2 font-semibold w-24">ADJ RET</th>
              </tr>
            </thead>
            <tbody>
              {pageItems.map((p) => (
                <tr
                  key={p.prediction_id}
                  className="border-b border-tape-border-soft hover:bg-tape-panel-2"
                >
                  <td className="px-4 py-2">
                    <TickerButton
                      ticker={p.ticker}
                      className="text-tape-text font-semibold hover:text-tape-accent"
                    />
                  </td>
                  <td className={`px-2 py-2 font-semibold ${dirColor(p.direction)}`}>
                    {p.direction === "bullish" ? "▲" : "▼"} {p.direction}
                  </td>
                  <td className="px-2 py-2 text-tape-sub">{p.confidence.toFixed(2)}</td>
                  <td className="px-2 py-2 text-tape-faint">{p.horizon_trading_days}d</td>
                  <td className="px-3 py-2 text-tape-faint">{fmtDate(p.issued_at)}</td>
                  <td className="px-3 py-2 max-w-[26rem]">
                    <div className="flex flex-col">
                      {p.headline ? (
                        isHttp(p.url) ? (
                          <a
                            href={p.url}
                            target="_blank"
                            rel="noopener noreferrer nofollow"
                            className="text-tape-sub hover:text-tape-accent truncate underline decoration-tape-border-soft underline-offset-2"
                            title={p.headline}
                          >
                            {p.headline}
                          </a>
                        ) : (
                          <span className="text-tape-sub truncate" title={p.headline}>
                            {p.headline}
                          </span>
                        )
                      ) : (
                        <span className="text-tape-faint">—</span>
                      )}
                      <span className="text-tape-dim text-[10px] flex gap-1.5">
                        {p.source_class && (
                          <span className="uppercase tracking-[0.08em]">{p.source_class}</span>
                        )}
                        {p.source && <span className="truncate max-w-[14rem]">· {p.source}</span>}
                      </span>
                    </div>
                  </td>
                  <td className="px-2 py-2 text-tape-muted">{p.status}</td>
                  <td className={`px-2 py-2 font-semibold ${outcomeColor(p.outcome)}`}>
                    {p.outcome ?? "—"}
                  </td>
                  <td
                    className={`px-2 py-2 ${
                      p.realized_adjusted_return == null
                        ? "text-tape-faint"
                        : p.realized_adjusted_return >= 0
                        ? "text-tape-bull"
                        : "text-tape-bear"
                    }`}
                  >
                    {p.realized_adjusted_return == null
                      ? "—"
                      : `${p.realized_adjusted_return >= 0 ? "+" : ""}${(p.realized_adjusted_return * 100).toFixed(2)}%`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!loading && reachable && laneItems.length > 0 && (
        <div className="shrink-0 flex items-center gap-4 px-[22px] py-2 border-t border-tape-border bg-tape-panel-2 tape-mono text-[11px] text-tape-faint">
          <span>
            showing{" "}
            <span className="text-tape-sub">
              {clampedPage * PAGE_SIZE + 1}–{clampedPage * PAGE_SIZE + pageItems.length}
            </span>{" "}
            of <span className="text-tape-sub">{laneItems.length}</span>
            {items.length >= FETCH_LIMIT && (
              <span className="text-tape-dim"> (most-recent {FETCH_LIMIT})</span>
            )}
          </span>
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => setPage((n) => Math.max(0, n - 1))}
              disabled={clampedPage === 0}
              className="px-2.5 py-1 rounded border border-tape-border text-tape-sub hover:text-tape-accent disabled:opacity-40 disabled:hover:text-tape-sub"
            >
              ← prev
            </button>
            <span>
              page <span className="text-tape-sub">{clampedPage + 1}</span> / {pageCount}
            </span>
            <button
              onClick={() => setPage((n) => Math.min(pageCount - 1, n + 1))}
              disabled={clampedPage >= pageCount - 1}
              className="px-2.5 py-1 rounded border border-tape-border text-tape-sub hover:text-tape-accent disabled:opacity-40 disabled:hover:text-tape-sub"
            >
              next →
            </button>
          </div>
        </div>
      )}

      <HealthStrip
        note={
          reachable
            ? "prediction ledger :8001 · immutable (I3/I4) · outcomes grader-filled · origin news via prediction_context"
            : "prediction API offline :8001"
        }
      />
    </>
  );
}
