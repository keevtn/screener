"use client";

import { Fragment, useEffect, useState } from "react";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import HealthStrip from "@/components/tape/HealthStrip";
import DeepDive from "@/components/tape/DeepDive";
import { TickerButton } from "@/components/tape/TickerModalProvider";
import {
  fetchEvidence,
  fetchModels,
  fetchRankings,
  fetchSpend,
  modelLabel,
  runRanking,
  type EvidenceCluster,
  type RankingRun,
  type Spend,
} from "@/lib/agents";

/**
 * TAPE_ AI Ranking surface (docs/ROADMAP.md Phase 7).
 *
 * Scheduled daily/weekly ranker runs land here, and the FORCE RUN control fires
 * an on-demand run with a selectable model + timeframe (trading-day horizon).
 * The ranker only PROPOSES a cited watchlist — this surface never changes config
 * or the prediction ledger.
 */

const HORIZONS = [1, 2, 3, 5, 10];
const CANDIDATE_OPTIONS = [25, 50, 75, 100, 150]; // breadth fed to the ranker
const DEFAULT_CANDIDATES = 50; // mirrors backend AGENT_RANKER_CANDIDATES default

function dirColor(d: string): string {
  if (d === "bullish") return "text-tape-bull";
  if (d === "bearish") return "text-tape-bear";
  return "text-tape-muted";
}

function sentColor(s: number | null): string {
  if (s == null) return "text-tape-faint";
  return s > 0.05 ? "text-tape-bull" : s < -0.05 ? "text-tape-bear" : "text-tape-muted";
}

function fmtDate(iso: string): string {
  return iso.slice(0, 16).replace("T", " ");
}

export default function RankPage() {
  const clock = useClock();

  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState<string>("");
  const [horizon, setHorizon] = useState<number>(3);
  const [candidates, setCandidates] = useState<number>(DEFAULT_CANDIDATES);
  const [highAlert, setHighAlert] = useState(true);
  const [extremeSentiment, setExtremeSentiment] = useState(true);

  const [runs, setRuns] = useState<RankingRun[]>([]);
  const [selected, setSelected] = useState<RankingRun | null>(null);
  const [spend, setSpend] = useState<Spend | null>(null);

  // Per-ranked-ticker evidence pulldown. Keyed by `${run_id}:${rank}` so the cache
  // survives switching runs; expanded holds which keys are open.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [evidence, setEvidence] = useState<Record<string, EvidenceCluster[] | "loading">>({});

  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reachable, setReachable] = useState(true);

  // DEEP DIVE: single-ticker AI analysis over our own data (own-data only).
  const [diveInput, setDiveInput] = useState("");
  const [diveTicker, setDiveTicker] = useState<string | null>(null);

  function submitDive(e: React.FormEvent) {
    e.preventDefault();
    const t = diveInput.trim().toUpperCase();
    if (t) setDiveTicker(t);
  }

  async function refresh() {
    try {
      const [rk, sp] = await Promise.all([fetchRankings(20), fetchSpend()]);
      setRuns(rk);
      setSpend(sp);
      setSelected((cur) => cur ?? rk[0] ?? null);
      setReachable(true);
    } catch {
      setReachable(false);
    }
  }

  useEffect(() => {
    fetchModels()
      .then((m) => {
        setModels(m.models);
        setModel(m.default);
      })
      .catch(() => setReachable(false));
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onForceRun() {
    if (!model) return;
    setRunning(true);
    setError(null);
    try {
      const run = await runRanking({
        model,
        horizon_trading_days: horizon,
        high_alert: highAlert,
        extreme_sentiment: extremeSentiment,
        limit: candidates,
      });
      setSelected(run);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  // Reset any open pulldowns when the selected run changes.
  useEffect(() => {
    setExpanded(new Set());
  }, [selected?.run_id]);

  async function toggleEvidence(run: RankingRun, rank: number, ids: string[]) {
    const key = `${run.run_id}:${rank}`;
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    if (evidence[key] === undefined) {
      setEvidence((e) => ({ ...e, [key]: "loading" }));
      try {
        const rows = await fetchEvidence(ids);
        setEvidence((e) => ({ ...e, [key]: rows }));
      } catch {
        setEvidence((e) => ({ ...e, [key]: [] }));
      }
    }
  }

  return (
    <>
      <TapeNav active="RANK" clock={clock} />

      {/* --- force-run control bar --- */}
      <div className="shrink-0 flex flex-wrap items-center gap-x-5 gap-y-2 px-[22px] py-3 border-b border-tape-border bg-tape-panel-2 tape-mono text-[11px]">
        <span className="text-tape-muted tracking-[0.12em] font-semibold">FORCE RUN</span>

        <label className="flex items-center gap-2 text-tape-faint">
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

        <label className="flex items-center gap-2 text-tape-faint">
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

        <label
          className="flex items-center gap-2 text-tape-faint"
          title="How many candidate tickers to feed the ranker. More breadth = wider watchlist at more input tokens (~450/candidate at Sonnet-5 ≈ $0.11/run for 50)."
        >
          candidates
          <select
            value={candidates}
            onChange={(e) => setCandidates(Number(e.target.value))}
            className="bg-tape-panel border border-tape-border rounded px-2 py-1 text-tape-text focus:border-tape-accent outline-none"
          >
            {CANDIDATE_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-1.5 text-tape-faint cursor-pointer">
          <input
            type="checkbox"
            checked={highAlert}
            onChange={(e) => setHighAlert(e.target.checked)}
            className="accent-tape-accent"
          />
          high_alert
        </label>
        <label className="flex items-center gap-1.5 text-tape-faint cursor-pointer">
          <input
            type="checkbox"
            checked={extremeSentiment}
            onChange={(e) => setExtremeSentiment(e.target.checked)}
            className="accent-tape-accent"
          />
          extreme_sentiment
        </label>

        <button
          onClick={onForceRun}
          disabled={running || !model || !reachable}
          className="ml-auto px-4 py-1.5 rounded border border-tape-accent text-tape-accent font-semibold tracking-[0.1em] hover:bg-tape-accent hover:text-tape-bg disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-tape-accent transition-colors"
        >
          {running ? "RANKING…" : "▶ RUN NOW"}
        </button>

        {spend && (
          <span className="text-tape-faint">
            spend today{" "}
            <span className="text-tape-sub">${spend.today_usd.toFixed(4)}</span> · {spend.calls} calls
          </span>
        )}
      </div>

      {error && (
        <div className="shrink-0 px-[22px] py-2 tape-mono text-[11px] text-tape-bear border-b border-tape-border-soft">
          run failed: {error}
        </div>
      )}

      {/* --- DEEP DIVE: pick a single ticker for an own-data AI analysis --- */}
      <div className="shrink-0 flex flex-wrap items-center gap-x-4 gap-y-2 px-[22px] py-2.5 border-b border-tape-border-soft bg-tape-panel tape-mono text-[11px]">
        <span className="text-tape-accent tracking-[0.14em] font-bold">◆ DEEP DIVE</span>
        <form onSubmit={submitDive} className="flex items-center gap-2">
          <input
            value={diveInput}
            onChange={(e) => setDiveInput(e.target.value)}
            placeholder="ticker e.g. NVDA"
            className="w-36 bg-tape-panel-2 border border-tape-border rounded px-2 py-1 text-tape-text uppercase placeholder:text-tape-dim placeholder:normal-case focus:border-tape-accent outline-none"
          />
          <button
            type="submit"
            disabled={!diveInput.trim()}
            className="px-3 py-1.5 rounded border border-tape-accent text-tape-accent font-semibold tracking-[0.1em] hover:bg-tape-accent hover:text-tape-bg disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-tape-accent transition-colors"
          >
            ANALYZE
          </button>
        </form>
        {diveTicker && (
          <button
            onClick={() => setDiveTicker(null)}
            className="text-tape-dim hover:text-tape-faint"
          >
            ✕ close
          </button>
        )}
        <span className="text-tape-dim ml-auto">
          own data only · 2 tickers / 5 min · persisted for instant revisits
        </span>
      </div>

      {diveTicker && (
        <div className="shrink-0 max-h-[46vh] overflow-y-auto px-[22px] py-3 border-b border-tape-border bg-tape-bg">
          <DeepDive ticker={diveTicker} />
        </div>
      )}

      {/* --- body: recent runs rail + selected run --- */}
      <div className="flex-1 flex overflow-hidden">
        <aside className="w-[220px] shrink-0 border-r border-tape-border-soft overflow-y-auto">
          <div className="px-4 py-2 tape-mono text-[10px] text-tape-muted tracking-[0.12em] border-b border-tape-border-soft">
            RECENT RUNS
          </div>
          {runs.length === 0 ? (
            <div className="px-4 py-3 tape-mono text-[10.5px] text-tape-faint">
              {reachable ? "no runs yet" : "prediction API offline (:8001)"}
            </div>
          ) : (
            runs.map((r) => (
              <button
                key={r.run_id}
                onClick={() => setSelected(r)}
                className={`w-full text-left px-4 py-2.5 tape-mono text-[10.5px] border-b border-tape-border-soft hover:bg-tape-panel-2 ${
                  selected?.run_id === r.run_id ? "bg-tape-panel-2" : ""
                }`}
              >
                <div className="flex justify-between text-tape-sub">
                  <span>{modelLabel(r.model)}</span>
                  <span
                    className={
                      r.status === "ok"
                        ? "text-tape-bull"
                        : r.status === "failed"
                        ? "text-tape-bear"
                        : "text-tape-muted"
                    }
                  >
                    {r.status}
                  </span>
                </div>
                <div className="text-tape-faint mt-0.5">
                  {r.trigger} · {r.horizon_trading_days}d · {r.items.length}/{r.candidate_count}
                </div>
                <div className="text-tape-dim mt-0.5">
                  {new Date(r.created_at).toLocaleString()}
                </div>
              </button>
            ))
          )}
        </aside>

        <section className="flex-1 overflow-y-auto">
          {!selected ? (
            <div className="h-full flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
              {reachable
                ? "No ranking selected. Hit RUN NOW to generate a cited watchlist."
                : "Prediction API not reachable on :8001 — start it with scripts/serve_api.py."}
            </div>
          ) : selected.status !== "ok" ? (
            <div className="h-full flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
              run {selected.status}
              {selected.error ? ` — ${selected.error}` : ""} · {selected.candidate_count} candidates
              screened
            </div>
          ) : (
            <table className="w-full border-collapse tape-mono text-[11px]">
              <caption className="text-left px-4 py-2 text-tape-dim text-[10px] tracking-[0.04em] border-b border-tape-border-soft">
                CONV = conviction — the model&apos;s 0–1 confidence in the directional call · click
                a row to see the aligned evidence
              </caption>
              <thead>
                <tr className="text-tape-muted text-left tracking-[0.1em] border-b border-tape-border">
                  <th className="px-4 py-2 font-semibold w-10">#</th>
                  <th className="px-3 py-2 font-semibold w-20">TICKER</th>
                  <th className="px-3 py-2 font-semibold w-20">DIR</th>
                  <th
                    className="px-3 py-2 font-semibold w-24"
                    title="Conviction: the model's confidence (0–1) in the directional call"
                  >
                    CONV
                  </th>
                  <th className="px-3 py-2 font-semibold">RATIONALE</th>
                  <th className="px-3 py-2 font-semibold w-16" title="Aligned evidence (click to expand)">
                    EVID
                  </th>
                </tr>
              </thead>
              <tbody>
                {selected.items.map((it) => {
                  const key = `${selected.run_id}:${it.rank}`;
                  const open = expanded.has(key);
                  const ev = evidence[key];
                  return (
                    <Fragment key={it.rank}>
                      <tr
                        onClick={() => toggleEvidence(selected, it.rank, it.evidence_ids)}
                        className={`border-b border-tape-border-soft hover:bg-tape-panel-2 cursor-pointer ${
                          open ? "bg-tape-panel-2" : ""
                        }`}
                      >
                        <td className="px-4 py-2.5 text-tape-faint">{it.rank}</td>
                        <td className="px-3 py-2.5 text-tape-text font-semibold">{it.ticker}</td>
                        <td className={`px-3 py-2.5 font-semibold ${dirColor(it.direction)}`}>
                          {it.direction}
                        </td>
                        <td className="px-3 py-2.5">
                          <div className="flex items-center gap-2">
                            <div className="w-12 h-1.5 bg-tape-border rounded overflow-hidden">
                              <div
                                className="h-full bg-tape-accent"
                                style={{ width: `${Math.round(it.conviction * 100)}%` }}
                              />
                            </div>
                            <span className="text-tape-sub">{it.conviction.toFixed(2)}</span>
                          </div>
                        </td>
                        <td className="px-3 py-2.5 text-tape-sub leading-snug">{it.rationale}</td>
                        <td className="px-3 py-2.5 text-tape-accent tabular-nums">
                          {it.evidence_ids.length > 0 ? (
                            <span>
                              {open ? "▾" : "▸"} {it.evidence_ids.length}
                            </span>
                          ) : (
                            <span className="text-tape-dim">—</span>
                          )}
                        </td>
                      </tr>
                      {open && (
                        <tr className="bg-tape-bg">
                          <td colSpan={6} className="px-4 py-2 border-b border-tape-border-soft">
                            <EvidenceList ev={ev} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <HealthStrip
        note={
          reachable
            ? `prediction API :8001 · ranker proposes only (never trades or edits config)`
            : "prediction API offline :8001"
        }
      />
    </>
  );
}

/** The aligned-evidence pulldown: cited clusters resolved to real headlines. */
function EvidenceList({ ev }: { ev: EvidenceCluster[] | "loading" | undefined }) {
  if (ev === "loading" || ev === undefined) {
    return <div className="tape-mono text-[10.5px] text-tape-muted py-1">resolving evidence…</div>;
  }
  if (ev.length === 0) {
    return (
      <div className="tape-mono text-[10.5px] text-tape-faint py-1">
        no resolvable evidence for this pick
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1.5 py-1">
      <div className="text-tape-muted text-[9.5px] tracking-[0.12em]">ALIGNED EVIDENCE</div>
      {ev.map((c) => (
        <div
          key={c.cluster_id}
          className="flex items-start gap-2 py-1 border-b border-tape-border-soft last:border-0"
        >
          <span className="shrink-0 mt-[1px] px-1.5 py-[1px] rounded border border-tape-border-soft text-tape-sub text-[9px] tracking-[0.04em]">
            {c.catalyst_type ?? "news"}
            {c.high_alert ? " ⚡" : ""}
          </span>
          <div className="flex-1 min-w-0">
            {c.url ? (
              <a
                href={c.url}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="text-tape-text hover:text-tape-accent hover:underline leading-snug"
              >
                {c.title ?? c.cluster_id}
              </a>
            ) : (
              <span className="text-tape-text leading-snug">{c.title ?? c.cluster_id}</span>
            )}
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 mt-0.5 text-[9.5px] text-tape-faint">
              <span>{c.source ?? "—"}</span>
              {c.source_class && <span className="text-tape-dim">· {c.source_class}</span>}
              <span className="text-tape-dim">· {fmtDate(c.published_at)}</span>
              <span className={sentColor(c.finbert_score)}>
                · sent {c.finbert_score == null ? "—" : c.finbert_score.toFixed(2)}
              </span>
              {c.materiality != null && (
                <span className="text-tape-muted">· mat {c.materiality.toFixed(2)}</span>
              )}
              {c.tickers.map((t) => (
                <TickerButton
                  key={t}
                  ticker={t}
                  className="text-tape-accent hover:underline"
                />
              ))}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
