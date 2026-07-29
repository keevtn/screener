"use client";

import { useEffect, useState } from "react";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import HealthStrip from "@/components/tape/HealthStrip";
import { fetchMetrics, type ConfigMetrics } from "@/lib/metrics";

/**
 * TAPE_ EVAL — the self-grading scoreboard (docs/ROADMAP.md Gate 5 = M2).
 *
 * One row per config_version: the live signal config alongside each shadow
 * baseline (always-up / random / momentum), so skill vs baselines is legible.
 * All numbers are what grade/metrics computed; "—" where undefined (e.g. hit
 * rate before anything resolves).
 */

function pct(v: number | null): string {
  return v == null ? "—" : `${(v * 100).toFixed(0)}%`;
}

function rateColor(v: number | null): string {
  if (v == null) return "text-tape-faint";
  if (v >= 0.55) return "text-tape-bull";
  if (v < 0.45) return "text-tape-bear";
  return "text-tape-sub";
}

export default function EvalPage() {
  const clock = useClock();
  const [items, setItems] = useState<ConfigMetrics[]>([]);
  const [reachable, setReachable] = useState(true);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const r = await fetchMetrics();
      if (cancelled) return;
      // Most-graded first so the config with real signal sits near the top.
      setItems([...r.items].sort((a, b) => b.total_graded - a.total_graded));
      setReachable(r.reachable);
      setLoading(false);
    }
    load();
    const t = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  return (
    <>
      <TapeNav active="EVAL" clock={clock} />

      <div className="shrink-0 flex flex-wrap items-center gap-x-5 gap-y-2 px-[22px] py-3 border-b border-tape-border bg-tape-panel-2 tape-mono text-[11px]">
        <span className="text-tape-muted tracking-[0.12em] font-semibold">EVAL</span>
        <span className="text-tape-faint">hit rate vs baselines, per config_version</span>
        <span className="text-tape-faint ml-auto">
          configs <span className="text-tape-sub">{items.length}</span>
        </span>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted">
          loading metrics…
        </div>
      ) : !reachable ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          Prediction API not reachable on :8001 — start it with scripts/serve_api.py.
        </div>
      ) : items.length === 0 ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          No graded predictions yet — metrics populate once the grader resolves the first horizons.
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
          <table className="w-full border-collapse tape-mono text-[11px]">
            <thead>
              <tr className="sticky top-0 bg-tape-panel-2 text-tape-muted text-left tracking-[0.1em] border-b border-tape-border z-10">
                <th className="px-4 py-2 font-semibold">CONFIG</th>
                <th className="px-2 py-2 font-semibold w-20">GRADED</th>
                <th className="px-2 py-2 font-semibold w-20">HIT RATE</th>
                <th className="px-2 py-2 font-semibold w-20">COVERAGE</th>
                <th className="px-2 py-2 font-semibold w-24">PREC ▲/▼</th>
                <th className="px-2 py-2 font-semibold w-24">RECALL ▲/▼</th>
                <th className="px-2 py-2 font-semibold w-20">LEAD d</th>
                <th className="px-3 py-2 font-semibold w-28">C / I / X</th>
              </tr>
            </thead>
            <tbody>
              {items.map((m) => (
                <tr
                  key={m.config_version}
                  className="border-b border-tape-border-soft hover:bg-tape-panel-2"
                >
                  <td className="px-4 py-2 text-tape-sub truncate max-w-[14rem]">
                    {m.config_version}
                  </td>
                  <td className="px-2 py-2 text-tape-text">{m.total_graded}</td>
                  <td className={`px-2 py-2 font-semibold ${rateColor(m.hit_rate)}`}>
                    {pct(m.hit_rate)}
                  </td>
                  <td className="px-2 py-2 text-tape-muted">{pct(m.coverage)}</td>
                  <td className="px-2 py-2 text-tape-faint">
                    {pct(m.precision.bullish)} / {pct(m.precision.bearish)}
                  </td>
                  <td className="px-2 py-2 text-tape-faint">
                    {pct(m.recall.bullish)} / {pct(m.recall.bearish)}
                  </td>
                  <td className="px-2 py-2 text-tape-muted">
                    {m.mean_lead_time_days == null ? "—" : m.mean_lead_time_days.toFixed(1)}
                  </td>
                  <td className="px-3 py-2 text-tape-dim">
                    <span className="text-tape-bull">{m.correct}</span> /{" "}
                    <span className="text-tape-bear">{m.incorrect}</span> /{" "}
                    <span className="text-tape-warn">{m.expired}</span>
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
            ? "metrics :8001 · hit rate = correct/resolved · coverage = resolved/graded"
            : "prediction API offline :8001"
        }
      />
    </>
  );
}
