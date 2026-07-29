/**
 * Client for the prediction API's CONFIG panel endpoints (task 7.3). The signal
 * config is an immutable, content-addressed blob: the current version + full
 * history are read-only, and the analyst's proposals sit in a pending queue.
 *
 * Approve/reject POST to the SAME human-gated path scripts/approve.py uses —
 * approval mints a new immutable version, rejection archives with a reason.
 * Nothing here auto-applies: this UI *is* the human gate (invariant I3).
 */

import { PRED_API } from "@/lib/config";

export type ConfigParams = Record<string, unknown>;

export interface CurrentConfig {
  config_version: string;
  created_at: string;
  notes: string | null;
  is_current: boolean;
  params: ConfigParams;
}

export interface ConfigVersion {
  config_version: string;
  created_at: string;
  notes: string | null;
  is_current: boolean;
  from_proposal: boolean;
}

export type ProposalStatus = "pending" | "approved" | "rejected";

export interface Proposal {
  id: string;
  created_at: string;
  status: ProposalStatus;
  base_config_version: string;
  /** JSON patch as {dotted.path: newValue}. */
  patch: Record<string, unknown>;
  rationale: string;
  report_md: string | null;
  resolved_at: string | null;
  resolved_reason: string | null;
  resulting_config_version: string | null;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${PRED_API}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return (await res.json()) as T;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${PRED_API}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch {
      /* keep the status-code detail */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const fetchCurrentConfig = () => getJSON<CurrentConfig>("/config/current");
export const fetchConfigVersions = () => getJSON<ConfigVersion[]>("/config/versions");
export const fetchConfigVersion = (v: string) =>
  getJSON<CurrentConfig>(`/config/versions/${encodeURIComponent(v)}`);

export async function fetchProposals(status?: ProposalStatus): Promise<Proposal[]> {
  const q = status ? `?status=${status}` : "";
  const r = await getJSON<{ count: number; items: Proposal[] }>(`/agents/proposals${q}`);
  return r.items;
}

/** Human gate: approve a proposal → the new immutable config version. */
export const approveProposal = (id: string, notes = "") =>
  postJSON<CurrentConfig>(`/config/proposals/${id}/approve`, { notes });

/** Human gate: reject a proposal with a required reason. */
export const rejectProposal = (id: string, reason: string) =>
  postJSON<{ id: string; status: string; resolved_reason: string | null }>(
    `/config/proposals/${id}/reject`,
    { reason },
  );

// --- rendering helpers -------------------------------------------------------

/** Resolve a dotted path (e.g. "armed.ttl_hours") against a params blob. */
export function valueAtPath(params: ConfigParams | undefined, path: string): unknown {
  if (!params) return undefined;
  let cur: unknown = params;
  for (const key of path.split(".")) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[key];
  }
  return cur;
}

/** Compact one-line rendering of a JSON value for the diff / params table. */
export function fmtValue(v: unknown): string {
  if (v === undefined) return "—";
  if (v === null) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export interface PatchRow {
  path: string;
  before: string;
  after: string;
}

/** A readable base→proposed diff for a proposal's patch, given the base params. */
export function patchRows(
  patch: Record<string, unknown>,
  baseParams: ConfigParams | undefined,
): PatchRow[] {
  return Object.entries(patch).map(([path, next]) => ({
    path,
    before: fmtValue(valueAtPath(baseParams, path)),
    after: fmtValue(next),
  }));
}
