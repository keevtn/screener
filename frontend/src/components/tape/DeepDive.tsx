"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchModels, modelLabel } from "@/lib/agents";
import {
  fetchAnalysis,
  runDeepDive,
  RateLimitError,
  type TickerAnalysis,
} from "@/lib/analysis";

/**
 * DEEP DIVE — single-ticker AI analysis over our OWN data (docs/ROADMAP.md 7.4).
 *
 * Self-contained TAPE_ panel: loads the persisted analysis for `ticker` instantly
 * (a revisit costs nothing), and a RUN / RE-RUN button fires a fresh model call with
 * a selectable model + timeframe. The analysis PROPOSES a view — it never trades or
 * edits config. Server-side rate limited to a couple of distinct tickers per 5 min.
 */

const HORIZONS = [1, 2, 3, 5, 10];

function dirColor(d: string | null): string {
  if (d === "bullish") return "text-tape-bull";
  if (d === "bearish") return "text-tape-bear";
  return "text-tape-muted";
}

export default function DeepDive({ ticker }: { ticker: string }) {
  const t = ticker.trim().toUpperCase();

  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [horizon, setHorizon] = useState(3);

  const [analysis, setAnalysis] = useState<TickerAnalysis | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    fetchModels()
      .then((m) => {
        setModels(m.models);
        setModel((cur) => cur || m.default);
      })
      .catch(() => setReachable(false));
  }, []);

  const load = useCallback(async () => {
    if (!t) return;
    setLoading(true);
    setError(null);
    try {
      const a = await fetchAnalysis(t);
      setAnalysis(a);
      setReachable(true);
    } catch {
      setReachable(false);
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  async function onRun() {
    if (!t || !model) return;
    setRunning(true);
    setError(null);
    try {
      const a = await runDeepDive(t, { model, horizon_trading_days: horizon });
      setAnalysis(a);
      setReachable(true);
    } catch (e) {
      if (e instanceof RateLimitError) {
        setError(
          `rate limited — try again in ${e.retryAfter}s (max 2 distinct tickers / 5 min)`
        );
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setRunning(false);
    }
  }

  const a = analysis;
  const hasRun = a && a.status === "ok";

  return (
    <div className="border border-tape-border rounded bg-tape-panel-2 tape-mono text-[11px]">
      {/* control bar */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3.5 py-2.5 border-b border-tape-border-soft">
        <span className="text-tape-accent tracking-[0.14em] font-bold">◆ DEEP DIVE</span>
        <span className="text-tape-text font-semibold tracking-[0.06em]">{t || "—"}</span>

        <label className="flex items-center gap-1.5 text-tape-faint">
          model
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="bg-tape-panel border border-tape-border rounded px-2 py-1 text-tape-text focus:border-tape-accent outline-none"
          >
            {models.map((m) => (
              <option key={m} value={m}>
                {modelLabel(m)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-tape-faint">
          timeframe
          <select
            value={horizon}
            onChange={(e) => setHorizon(Number(e.target.value))}
            className="bg-tape-panel border border-tape-border rounded px-2 py-1 text-tape-text focus:border-tape-accent outline-none"
          >
            {HORIZONS.map((h) => (
              <option key={h} value={h}>
                {h}d
              </option>
            ))}
          </select>
        </label>

        <button
          onClick={onRun}
          disabled={running || !t || !model || !reachable}
          className="ml-auto px-3.5 py-1.5 rounded border border-tape-accent text-tape-accent font-semibold tracking-[0.1em] hover:bg-tape-accent hover:text-tape-bg disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-tape-accent transition-colors"
        >
          {running ? "ANALYZING…" : hasRun ? "▶ RE-RUN" : "▶ RUN"}
        </button>
      </div>

      {error && (
        <div className="px-3.5 py-2 text-tape-bear border-b border-tape-border-soft">{error}</div>
      )}

      {/* body */}
      <div className="px-3.5 py-3">
        {loading ? (
          <div className="text-tape-muted py-4 text-center">loading {t}…</div>
        ) : !reachable ? (
          <div className="text-tape-muted py-4 text-center">
            prediction API offline (:8001) — start it with scripts/serve_api.py
          </div>
        ) : !a ? (
          <div className="text-tape-faint py-4 text-center">
            No analysis yet for {t}. Hit RUN to generate an own-data AI read.
          </div>
        ) : a.status === "empty" ? (
          <div className="text-tape-faint py-4 text-center">
            No own-data evidence for {t} yet (no scored clusters / attention). Nothing to analyze.
          </div>
        ) : a.status === "failed" ? (
          <div className="text-tape-bear py-4 text-center">
            analysis failed{a.error ? ` — ${a.error}` : ""}
          </div>
        ) : (
          <div className="flex flex-col gap-3.5">
            {/* verdict row */}
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
              <span className={`text-[13px] font-bold tracking-[0.08em] ${dirColor(a.direction)}`}>
                {(a.direction ?? "neutral").toUpperCase()}
              </span>
              <span className="flex items-center gap-2 text-tape-faint">
                conviction
                <span className="inline-block w-16 h-1.5 bg-tape-border rounded overflow-hidden align-middle">
                  <span
                    className="block h-full bg-tape-accent"
                    style={{ width: `${Math.round((a.conviction ?? 0) * 100)}%` }}
                  />
                </span>
                <span className="text-tape-sub">{(a.conviction ?? 0).toFixed(2)}</span>
              </span>
              <span className="text-tape-dim ml-auto">
                {modelLabel(a.model)} · {a.horizon_trading_days}d ·{" "}
                {new Date(a.created_at).toLocaleString()}
              </span>
            </div>

            {/* thesis */}
            <p className="text-tape-text leading-relaxed">{a.thesis}</p>

            {/* key evidence */}
            {a.key_evidence.length > 0 && (
              <div>
                <div className="text-tape-muted tracking-[0.12em] text-[10px] mb-1">KEY EVIDENCE</div>
                <ul className="flex flex-col gap-1">
                  {a.key_evidence.map((e, i) => (
                    <li key={i} className="flex gap-2 text-tape-sub leading-snug">
                      <span className="text-tape-accent">+</span>
                      <span>
                        {e.point}
                        {e.cluster_id && (
                          <span className="text-tape-dim ml-1.5">[{e.cluster_id.slice(0, 10)}]</span>
                        )}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* two-up: risks / what would change */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {a.risks.length > 0 && (
                <div>
                  <div className="text-tape-muted tracking-[0.12em] text-[10px] mb-1">RISKS</div>
                  <ul className="flex flex-col gap-1">
                    {a.risks.map((r, i) => (
                      <li key={i} className="flex gap-2 text-tape-faint leading-snug">
                        <span className="text-tape-warn">!</span>
                        <span>{r}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {a.what_would_change_my_mind.length > 0 && (
                <div>
                  <div className="text-tape-muted tracking-[0.12em] text-[10px] mb-1">
                    WHAT WOULD CHANGE MY MIND
                  </div>
                  <ul className="flex flex-col gap-1">
                    {a.what_would_change_my_mind.map((w, i) => (
                      <li key={i} className="flex gap-2 text-tape-faint leading-snug">
                        <span className="text-tape-sub">?</span>
                        <span>{w}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>

            {/* provenance: the clusters this read was grounded in */}
            {a.evidence?.clusters && a.evidence.clusters.length > 0 && (
              <details className="border-t border-tape-border-soft pt-2">
                <summary className="text-tape-dim cursor-pointer hover:text-tape-faint">
                  evidence read · {a.evidence.clusters.length} clusters ·{" "}
                  window {a.evidence.window?.item_count ?? 0} items
                  {a.evidence.next_earnings ? ` · next earnings ${a.evidence.next_earnings}` : ""}
                </summary>
                <ul className="mt-2 flex flex-col gap-1.5">
                  {a.evidence.clusters.slice(0, 8).map((c) => (
                    <li key={c.cluster_id} className="text-tape-faint leading-snug">
                      <span className="text-tape-dim">[{c.cluster_id.slice(0, 10)}]</span>{" "}
                      <span className="text-tape-sub">{c.title ?? "(untitled)"}</span>
                      <span className="text-tape-dim">
                        {" "}
                        · {c.source} · {c.catalyst_type ?? "—"}
                      </span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
