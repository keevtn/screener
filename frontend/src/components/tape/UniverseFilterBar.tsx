"use client";

import { useState, type ReactNode } from "react";

/**
 * UNIVERSE composable filter builder. "+ add filter" opens a menu of every
 * server-side field; each pick adds an editable chip and all active chips
 * AND-combine into one /universe/screen query (interjoin sector AND cap AND
 * price AND short AND has-signal AND earnings-window …). Sort stays a separate
 * control on the page. Mirrors the news SCREENER's chip pattern, mapped to the
 * universe's range/categorical/boolean/day-window field kinds.
 */

export type FieldKind = "range" | "cat" | "bool" | "days";
export type UniOp = "min" | "max" | "eq" | "is" | "within";

export interface UniverseField {
  key: string;
  label: string;
  kind: FieldKind;
  unit?: string;
  step?: number;
  /** range fields: which bounds are offered (server lacks a max for some). */
  ops?: ("min" | "max")[];
  /** cat fields: dropdown options. */
  options?: string[];
}

export interface ActiveUniFilter {
  id: string;
  field: string;
  op: UniOp;
  value: number | string;
}

const OP_SYM: Record<UniOp, string> = { min: "≥", max: "≤", eq: "=", is: "", within: "≤" };

export default function UniverseFilterBar({
  fields,
  filters,
  matched,
  total,
  onAdd,
  onUpdate,
  onRemove,
  searchSlot,
}: {
  fields: UniverseField[];
  filters: ActiveUniFilter[];
  matched: number;
  total: number;
  onAdd: (field: string, op: UniOp) => void;
  onUpdate: (id: string, value: number | string) => void;
  onRemove: (id: string) => void;
  /** Whole-universe ticker/name search (rendered between the builder and the count). */
  searchSlot?: ReactNode;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const fieldOf = (k: string) => fields.find((f) => f.key === k);

  return (
    <div className="shrink-0 flex items-center gap-2.5 px-[22px] py-3 flex-wrap bg-tape-panel-2 border-b border-tape-border tape-mono text-[11px]">
      <span className="text-tape-muted tracking-[0.12em] font-semibold">UNIVERSE</span>

      {filters.map((f) => {
        const fld = fieldOf(f.field);
        return (
          <span
            key={f.id}
            className="flex items-center gap-1 rounded-[14px] px-2.5 py-[4px] font-medium text-tape-accent border border-[#1F3B38] bg-[rgba(79,209,197,0.06)]"
          >
            {fld?.label ?? f.field} {OP_SYM[f.op]}
            {fld?.kind === "cat" ? (
              <select
                value={String(f.value)}
                onChange={(e) => onUpdate(f.id, e.target.value)}
                className="bg-tape-panel border border-tape-border rounded px-1 py-0.5 text-tape-text outline-none focus:border-tape-accent max-w-[9rem]"
              >
                <option value="">any</option>
                {(fld?.options ?? []).map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            ) : fld?.kind === "bool" ? null : (
              <>
                <input
                  type="number"
                  step={fld?.step ?? 1}
                  value={Number(f.value)}
                  onChange={(e) => onUpdate(f.id, Number(e.target.value))}
                  className="w-14 bg-tape-panel border border-tape-border rounded px-1 py-0.5 text-tape-text outline-none focus:border-tape-accent tabular-nums"
                />
                {fld?.unit && <span className="text-tape-dim">{fld.unit}</span>}
              </>
            )}
            <button
              onClick={() => onRemove(f.id)}
              className="text-[#3E7B74] hover:text-tape-bear ml-0.5"
              title="remove filter"
            >
              ×
            </button>
          </span>
        );
      })}

      <div className="relative">
        <button
          onClick={() => setMenuOpen((o) => !o)}
          className="rounded-[14px] px-3 py-[5px] font-medium text-tape-sub border border-dashed border-tape-border hover:border-tape-accent hover:text-tape-accent transition-colors"
        >
          + add filter
        </button>
        {menuOpen && (
          <>
            <div className="fixed inset-0 z-20" onClick={() => setMenuOpen(false)} />
            <div className="absolute top-full left-0 mt-1 z-30 bg-tape-panel border border-tape-border rounded shadow-lg py-1 min-w-[200px] max-h-96 overflow-auto">
              {fields.flatMap((fld) => {
                const entries: { op: UniOp; suffix: string }[] =
                  fld.kind === "range"
                    ? (fld.ops ?? ["min", "max"]).map((op) => ({ op, suffix: OP_SYM[op] }))
                    : fld.kind === "cat"
                    ? [{ op: "eq", suffix: "=" }]
                    : fld.kind === "bool"
                    ? [{ op: "is", suffix: "" }]
                    : [{ op: "within", suffix: "≤" }];
                return entries.map(({ op, suffix }) => (
                  <button
                    key={fld.key + op}
                    onClick={() => {
                      onAdd(fld.key, op);
                      setMenuOpen(false);
                    }}
                    className="block w-full text-left px-3 py-1.5 text-[10.5px] text-tape-faint hover:bg-tape-panel-2 hover:text-tape-accent"
                  >
                    {fld.label} {suffix}
                    {fld.unit ? ` (${fld.unit})` : ""}
                  </button>
                ));
              })}
            </div>
          </>
        )}
      </div>

      {filters.length > 0 && (
        <button
          onClick={() => filters.forEach((f) => onRemove(f.id))}
          className="text-tape-dim hover:text-tape-bear text-[10px]"
          title="clear all filters"
        >
          clear all
        </button>
      )}

      {searchSlot}

      <span className="ml-auto font-medium text-tape-muted tabular-nums">
        <span className="text-tape-text font-bold">{matched.toLocaleString()}</span> /{" "}
        {total.toLocaleString()}
      </span>
    </div>
  );
}
