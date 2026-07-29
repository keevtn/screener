"use client";

import { useCallback, useEffect, useState } from "react";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import HealthStrip from "@/components/tape/HealthStrip";
import MiniMarkdown from "@/components/tape/MiniMarkdown";
import { fetchSpend, type Spend } from "@/lib/agents";
import {
  approveProposal,
  fetchConfigVersion,
  fetchConfigVersions,
  fetchCurrentConfig,
  fetchProposals,
  patchRows,
  rejectProposal,
  type ConfigParams,
  type ConfigVersion,
  type CurrentConfig,
  type Proposal,
} from "@/lib/tapeconfig";

/**
 * TAPE_ CONFIG panel (docs/ROADMAP.md task 7.3).
 *
 * The signal config is an immutable, content-addressed blob. This surface shows
 * the current version + full params, the version history, and the analyst's
 * pending-changes queue (proposal markdown + a readable JSON-patch diff). Approve
 * / reject call the SAME human-gated path scripts/approve.py uses: approval mints
 * a new immutable version, rejection archives with a reason. Nothing auto-applies
 * — this UI is the human gate (invariant I3).
 */

function shortVer(v: string | null): string {
  if (!v) return "—";
  return v.startsWith("cfg-") ? v.slice(0, 11) : v.slice(0, 10);
}

function statusColor(s: string): string {
  if (s === "pending") return "text-tape-warn";
  if (s === "approved") return "text-tape-bull";
  return "text-tape-bear"; // rejected
}

export default function ConfigPage() {
  const clock = useClock();

  const [current, setCurrent] = useState<CurrentConfig | null>(null);
  const [versions, setVersions] = useState<ConfigVersion[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [spend, setSpend] = useState<Spend | null>(null);
  const [reachable, setReachable] = useState(true);

  // selection: either a proposal (review) or a config version (params view).
  const [selProposal, setSelProposal] = useState<string | null>(null);
  const [selVersion, setSelVersion] = useState<string | null>(null);
  const [paramsByVer, setParamsByVer] = useState<Record<string, ConfigParams>>({});

  const [notes, setNotes] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadParams = useCallback(
    async (ver: string) => {
      if (paramsByVer[ver]) return;
      try {
        const c = await fetchConfigVersion(ver);
        setParamsByVer((m) => ({ ...m, [ver]: c.params }));
      } catch {
        /* leave uncached; diff falls back to "—" */
      }
    },
    [paramsByVer],
  );

  const refresh = useCallback(async () => {
    try {
      const [cur, vers, props, sp] = await Promise.all([
        fetchCurrentConfig(),
        fetchConfigVersions(),
        fetchProposals(),
        fetchSpend(),
      ]);
      setCurrent(cur);
      setVersions(vers);
      setProposals(props);
      setSpend(sp);
      setParamsByVer((m) => ({ ...m, [cur.config_version]: cur.params }));
      setReachable(true);
    } catch {
      setReachable(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const proposal = proposals.find((p) => p.id === selProposal) ?? null;

  // When a proposal is selected, make sure its base params are loaded for the diff.
  useEffect(() => {
    if (proposal) loadParams(proposal.base_config_version);
  }, [proposal, loadParams]);

  function pickProposal(id: string) {
    setSelProposal(id);
    setSelVersion(null);
    setNotes("");
    setReason("");
    setError(null);
  }
  function pickVersion(v: string) {
    setSelVersion(v);
    setSelProposal(null);
    setError(null);
    loadParams(v);
  }

  async function onApprove() {
    if (!proposal) return;
    setBusy(true);
    setError(null);
    try {
      await approveProposal(proposal.id, notes);
      await refresh();
      setSelProposal(proposal.id); // keep it selected to show the resolved state
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function onReject() {
    if (!proposal || !reason.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await rejectProposal(proposal.id, reason.trim());
      await refresh();
      setSelProposal(proposal.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const pendingCount = proposals.filter((p) => p.status === "pending").length;
  const paramsView =
    selVersion != null
      ? paramsByVer[selVersion]
      : selProposal == null
      ? current?.params
      : undefined;
  const paramsViewVer = selVersion ?? (selProposal == null ? current?.config_version : undefined);

  return (
    <>
      <TapeNav active="CONFIG" clock={clock} />

      {/* --- header strip: current version + spend-vs-cap meter --- */}
      <div className="shrink-0 flex flex-wrap items-center gap-x-6 gap-y-2 px-[22px] py-3 border-b border-tape-border bg-tape-panel-2 tape-mono text-[11px]">
        <span className="text-tape-muted tracking-[0.12em] font-semibold">CURRENT CONFIG</span>
        <span className="text-tape-text font-semibold">{shortVer(current?.config_version ?? null)}</span>
        {current?.notes && <span className="text-tape-dim">{current.notes}</span>}
        <span className="text-tape-faint">
          {versions.length} version{versions.length === 1 ? "" : "s"} · {pendingCount} pending
        </span>

        {spend && (
          <div className="ml-auto flex items-center gap-2 text-tape-faint">
            <span>LLM spend today</span>
            <div className="w-28 h-1.5 bg-tape-border rounded overflow-hidden" title="daily soft cap">
              <div
                className={`h-full ${spend.pct_of_cap >= 1 ? "bg-tape-bear" : "bg-tape-accent"}`}
                style={{ width: `${Math.min(100, Math.round(spend.pct_of_cap * 100))}%` }}
              />
            </div>
            <span className="text-tape-sub tabular-nums">
              ${spend.today_usd.toFixed(4)} / ${spend.cap_usd.toFixed(2)}
            </span>
          </div>
        )}
      </div>

      {error && (
        <div className="shrink-0 px-[22px] py-2 tape-mono text-[11px] text-tape-bear border-b border-tape-border-soft">
          {error}
        </div>
      )}

      {/* --- body: queue + version rail | detail --- */}
      <div className="flex-1 flex overflow-hidden">
        <aside className="w-[240px] shrink-0 border-r border-tape-border-soft overflow-y-auto">
          <div className="px-4 py-2 tape-mono text-[10px] text-tape-muted tracking-[0.12em] border-b border-tape-border-soft">
            PROPOSAL QUEUE
          </div>
          {proposals.length === 0 ? (
            <div className="px-4 py-3 tape-mono text-[10.5px] text-tape-faint">
              {reachable ? "no proposals" : "prediction API offline (:8001)"}
            </div>
          ) : (
            proposals.map((p) => (
              <button
                key={p.id}
                onClick={() => pickProposal(p.id)}
                className={`w-full text-left px-4 py-2.5 tape-mono text-[10.5px] border-b border-tape-border-soft hover:bg-tape-panel-2 ${
                  selProposal === p.id ? "bg-tape-panel-2" : ""
                }`}
              >
                <div className="flex justify-between">
                  <span className="text-tape-sub">{Object.keys(p.patch).length} change(s)</span>
                  <span className={statusColor(p.status)}>{p.status}</span>
                </div>
                <div className="text-tape-dim mt-0.5">{new Date(p.created_at).toLocaleString()}</div>
              </button>
            ))
          )}

          <div className="px-4 py-2 tape-mono text-[10px] text-tape-muted tracking-[0.12em] border-b border-t border-tape-border-soft">
            CONFIG VERSIONS
          </div>
          {versions.map((v) => (
            <button
              key={v.config_version}
              onClick={() => pickVersion(v.config_version)}
              className={`w-full text-left px-4 py-2 tape-mono text-[10.5px] border-b border-tape-border-soft hover:bg-tape-panel-2 ${
                selVersion === v.config_version ? "bg-tape-panel-2" : ""
              }`}
            >
              <div className="flex justify-between">
                <span className="text-tape-sub">{shortVer(v.config_version)}</span>
                <span className="flex gap-1">
                  {v.is_current && <span className="text-tape-accent">current</span>}
                  {v.from_proposal && <span className="text-tape-bull">✓prop</span>}
                </span>
              </div>
              <div className="text-tape-dim mt-0.5">{new Date(v.created_at).toLocaleDateString()}</div>
            </button>
          ))}
        </aside>

        <section className="flex-1 overflow-y-auto" tabIndex={0} aria-label="Configuration detail">
          {proposal ? (
            <ProposalDetail
              proposal={proposal}
              baseParams={paramsByVer[proposal.base_config_version]}
              notes={notes}
              reason={reason}
              busy={busy}
              onNotes={setNotes}
              onReason={setReason}
              onApprove={onApprove}
              onReject={onReject}
            />
          ) : paramsView && paramsViewVer ? (
            <div className="px-6 py-4">
              <div className="flex items-center gap-3 mb-3 tape-mono">
                <span className="text-tape-text text-[13px] font-bold">{paramsViewVer}</span>
                {paramsViewVer === current?.config_version && (
                  <span className="text-[9.5px] text-tape-accent border border-tape-border rounded px-1.5 py-[2px] tracking-[0.1em]">
                    CURRENT
                  </span>
                )}
                <span className="text-tape-dim text-[10px]">immutable params blob</span>
              </div>
              <pre className="tape-mono text-[10.5px] leading-relaxed text-tape-sub bg-tape-panel-2 border border-tape-border-soft rounded p-3 overflow-x-auto whitespace-pre">
                {JSON.stringify(paramsView, null, 2)}
              </pre>
            </div>
          ) : (
            <div className="h-full flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
              {reachable
                ? "Pick a proposal to review, or a version to inspect its params."
                : "Prediction API not reachable on :8001 — start it with scripts/serve_api.py."}
            </div>
          )}
        </section>
      </div>

      <HealthStrip
        note={
          reachable
            ? "prediction API :8001 · approvals mint immutable versions · nothing auto-applies (I3)"
            : "prediction API offline :8001"
        }
      />
    </>
  );
}

function ProposalDetail({
  proposal,
  baseParams,
  notes,
  reason,
  busy,
  onNotes,
  onReason,
  onApprove,
  onReject,
}: {
  proposal: Proposal;
  baseParams: ConfigParams | undefined;
  notes: string;
  reason: string;
  busy: boolean;
  onNotes: (s: string) => void;
  onReason: (s: string) => void;
  onApprove: () => void;
  onReject: () => void;
}) {
  const rows = patchRows(proposal.patch, baseParams);
  const pending = proposal.status === "pending";

  return (
    <div className="px-6 py-4 tape-mono text-[11px]">
      <div className="flex items-center gap-3 mb-1">
        <span className="text-tape-text text-[13px] font-bold">PROPOSAL</span>
        <span className={`${statusColor(proposal.status)} font-semibold tracking-[0.08em]`}>
          {proposal.status.toUpperCase()}
        </span>
        <span className="text-tape-dim text-[10px]">
          base {shortVer(proposal.base_config_version)} · {new Date(proposal.created_at).toLocaleString()}
        </span>
      </div>
      {proposal.rationale && <div className="text-tape-sub mb-3">{proposal.rationale}</div>}

      {/* JSON-patch diff: base → proposed */}
      <div className="text-tape-muted text-[10px] tracking-[0.12em] mb-1">PROPOSED CHANGES</div>
      <table className="w-full border-collapse mb-4">
        <thead>
          <tr className="text-tape-muted text-left border-b border-tape-border">
            <th className="px-2 py-1 font-semibold">param</th>
            <th className="px-2 py-1 font-semibold">current</th>
            <th className="px-2 py-1 font-semibold w-6"></th>
            <th className="px-2 py-1 font-semibold">proposed</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.path} className="border-b border-tape-border-soft">
              <td className="px-2 py-1.5 text-tape-text">{r.path}</td>
              <td className="px-2 py-1.5 text-tape-faint tabular-nums">{r.before}</td>
              <td className="px-2 py-1.5 text-tape-dim">→</td>
              <td className="px-2 py-1.5 text-tape-accent tabular-nums">{r.after}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* analyst report markdown */}
      {proposal.report_md && (
        <>
          <div className="text-tape-muted text-[10px] tracking-[0.12em] mb-1">ANALYST REPORT</div>
          <div className="bg-tape-panel-2 border border-tape-border-soft rounded p-3 mb-4">
            <MiniMarkdown text={proposal.report_md} />
          </div>
        </>
      )}

      {/* resolved state */}
      {!pending && (
        <div className="text-tape-faint mb-4">
          {proposal.status === "approved" ? (
            <>
              minted <span className="text-tape-bull">{shortVer(proposal.resulting_config_version)}</span>
              {proposal.resolved_at && ` · ${new Date(proposal.resolved_at).toLocaleString()}`}
            </>
          ) : (
            <>
              rejected: <span className="text-tape-bear">{proposal.resolved_reason ?? "—"}</span>
              {proposal.resolved_at && ` · ${new Date(proposal.resolved_at).toLocaleString()}`}
            </>
          )}
        </div>
      )}

      {/* human gate: approve / reject */}
      {pending && (
        <div className="border-t border-tape-border pt-4 flex flex-col gap-3 max-w-xl">
          <div className="flex items-center gap-2">
            <input
              value={notes}
              onChange={(e) => onNotes(e.target.value)}
              placeholder="approval note (optional)"
              className="flex-1 bg-tape-panel border border-tape-border rounded px-2 py-1.5 text-tape-text placeholder:text-tape-dim focus:border-tape-accent outline-none"
            />
            <button
              onClick={onApprove}
              disabled={busy}
              className="px-4 py-1.5 rounded border border-tape-bull text-tape-bull font-semibold tracking-[0.1em] hover:bg-tape-bull hover:text-tape-bg disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-tape-bull transition-colors"
            >
              {busy ? "…" : "✓ APPROVE"}
            </button>
          </div>
          <div className="flex items-center gap-2">
            <input
              value={reason}
              onChange={(e) => onReason(e.target.value)}
              placeholder="rejection reason (required)"
              className="flex-1 bg-tape-panel border border-tape-border rounded px-2 py-1.5 text-tape-text placeholder:text-tape-dim focus:border-tape-accent outline-none"
            />
            <button
              onClick={onReject}
              disabled={busy || !reason.trim()}
              className="px-4 py-1.5 rounded border border-tape-bear text-tape-bear font-semibold tracking-[0.1em] hover:bg-tape-bear hover:text-tape-bg disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-tape-bear transition-colors"
            >
              ✕ REJECT
            </button>
          </div>
          <div className="text-tape-dim text-[10px]">
            approval mints a new immutable config version · nothing auto-applies
          </div>
        </div>
      )}
    </div>
  );
}
