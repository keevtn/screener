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
 * grader wrote — nothing synthesized.
 */

const STATUSES = ["all", "open", "graded"] as const;
type StatusFilter = (typeof STATUSES)[number];

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

export default function LedgerPage() {
  const clock = useClock();
  const [status, setStatus] = useState<StatusFilter>("all");
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

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const r = await fetchLedger({ status: status === "all" ? undefined : status, limit: 300 });
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

  const stats = useMemo(() => {
    const graded = items.filter((p) => p.status === "graded");
    const resolved = graded.filter((p) => p.outcome === "correct" || p.outcome === "incorrect");
    const correct = graded.filter((p) => p.outcome === "correct").length;
    return {
      total: items.length,
      open: items.filter((p) => p.status === "open").length,
      graded: graded.length,
      hitRate: resolved.length ? correct / resolved.length : null,
    };
  }, [items]);

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
      ) : items.length === 0 ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          No predictions in the ledger for this filter.
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
                <th className="px-3 py-2 font-semibold">CONFIG</th>
                <th className="px-2 py-2 font-semibold w-20">STATUS</th>
                <th className="px-2 py-2 font-semibold w-24">OUTCOME</th>
                <th className="px-2 py-2 font-semibold w-24">ADJ RET</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => (
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
                  <td className="px-3 py-2 text-tape-dim truncate max-w-[10rem]">
                    {p.config_version}
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

      <HealthStrip
        note={
          reachable
            ? "prediction ledger :8001 · immutable (I3/I4) · outcomes grader-filled"
            : "prediction API offline :8001"
        }
      />
    </>
  );
}
