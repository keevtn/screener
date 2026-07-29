"use client";

import { type ReactNode } from "react";
import { Density } from "@/lib/tape/types";

/**
 * SCREENER filter bar — an ALWAYS-VISIBLE, Finviz-style compact grid (TAPE
 * aesthetic). Every filter is exposed at once (no "+ add filter" builder): the
 * whole set is laid out as small labelled number inputs / dropdowns / a toggle
 * that AND-combine live as you type. The underlying filter engine is unchanged —
 * this is presentation over a structured `ScreenerFilters` state.
 */

export interface ScreenerFilters {
  mentionsMin: number | null;
  priceMin: number | null;
  priceMax: number | null;
  pctMin: number | null;
  pctMax: number | null;
  volMin: number | null; // today's volume, M shares
  avgVolMin: number | null; // avg volume, M shares
  mcapMin: number | null; // $B
  mcapMax: number | null; // $B
  sector: string; // "" = any
  industry: string; // "" = any
  sentMin: number | null;
  heatMin: number | null;
  buzzMin: number | null;
  hasSignal: boolean;
}

export const EMPTY_FILTERS: ScreenerFilters = {
  mentionsMin: null,
  priceMin: null,
  priceMax: null,
  pctMin: null,
  pctMax: null,
  volMin: null,
  avgVolMin: null,
  mcapMin: null,
  mcapMax: null,
  sector: "",
  industry: "",
  sentMin: null,
  heatMin: null,
  buzzMin: null,
  hasSignal: false,
};

/** Count of non-empty constraints — the "active filters" readout. */
export function activeFilterCount(f: ScreenerFilters): number {
  let n = 0;
  for (const [k, v] of Object.entries(f)) {
    if (k === "hasSignal") n += v ? 1 : 0;
    else if (typeof v === "string") n += v !== "" ? 1 : 0;
    else n += v != null ? 1 : 0;
  }
  return n;
}

const numOr = (s: string): number | null => (s.trim() === "" ? null : Number(s));

const INPUT =
  "w-[3.1rem] bg-tape-panel border border-tape-border rounded px-1 py-0.5 " +
  "text-tape-text outline-none focus:border-tape-accent tabular-nums text-[10.5px]";
const LABEL = "text-tape-dim tracking-[0.08em] text-[9px] font-semibold whitespace-nowrap";

function Cell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="flex items-center gap-1">
      <span className={LABEL}>{label}</span>
      {children}
    </span>
  );
}

function NumMin({
  label,
  unit,
  step,
  value,
  onChange,
}: {
  label: string;
  unit?: string;
  step?: number;
  value: number | null;
  onChange: (v: number | null) => void;
}) {
  return (
    <Cell label={label}>
      <span className="text-tape-faint text-[10px]">≥</span>
      <input
        type="number"
        step={step ?? 1}
        value={value ?? ""}
        placeholder="—"
        onChange={(e) => onChange(numOr(e.target.value))}
        className={INPUT}
      />
      {unit && <span className="text-tape-dim text-[9px]">{unit}</span>}
    </Cell>
  );
}

function NumRange({
  label,
  unit,
  step,
  min,
  max,
  onMin,
  onMax,
}: {
  label: string;
  unit?: string;
  step?: number;
  min: number | null;
  max: number | null;
  onMin: (v: number | null) => void;
  onMax: (v: number | null) => void;
}) {
  return (
    <Cell label={label}>
      <input
        type="number"
        step={step ?? 1}
        value={min ?? ""}
        placeholder="min"
        onChange={(e) => onMin(numOr(e.target.value))}
        className={INPUT}
      />
      <span className="text-tape-faint text-[10px]">–</span>
      <input
        type="number"
        step={step ?? 1}
        value={max ?? ""}
        placeholder="max"
        onChange={(e) => onMax(numOr(e.target.value))}
        className={INPUT}
      />
      {unit && <span className="text-tape-dim text-[9px]">{unit}</span>}
    </Cell>
  );
}

function Sel({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  return (
    <Cell label={label}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        className="max-w-[8.5rem] bg-tape-panel border border-tape-border rounded px-1 py-0.5 text-tape-text text-[10.5px] outline-none focus:border-tape-accent"
      >
        <option value="">any</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </Cell>
  );
}

export default function ScreenerFilterBar({
  filters,
  set,
  onClear,
  sectors,
  industries,
  matched,
  total,
  live,
  density,
  onToggleDensity,
  searchSlot,
}: {
  filters: ScreenerFilters;
  set: (patch: Partial<ScreenerFilters>) => void;
  onClear: () => void;
  sectors: string[];
  industries: string[];
  matched: number;
  total: number;
  live: boolean;
  density: Density;
  onToggleDensity: () => void;
  searchSlot?: ReactNode;
}) {
  const f = filters;
  const active = activeFilterCount(f);
  return (
    <div className="sticky top-12 z-20 flex flex-col gap-2 px-[22px] py-2.5 bg-tape-panel border-b border-tape-border-soft">
      {/* the filter grid — wraps to as many rows as the width needs (no squeeze) */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <NumMin label="MENT" value={f.mentionsMin} onChange={(v) => set({ mentionsMin: v })} />
        <NumRange
          label="PRICE"
          unit="$"
          min={f.priceMin}
          max={f.priceMax}
          onMin={(v) => set({ priceMin: v })}
          onMax={(v) => set({ priceMax: v })}
        />
        <NumRange
          label="%CHG"
          unit="%"
          step={0.5}
          min={f.pctMin}
          max={f.pctMax}
          onMin={(v) => set({ pctMin: v })}
          onMax={(v) => set({ pctMax: v })}
        />
        <NumMin label="VOL" unit="M" step={0.5} value={f.volMin} onChange={(v) => set({ volMin: v })} />
        <NumMin
          label="AVG VOL"
          unit="M"
          step={0.5}
          value={f.avgVolMin}
          onChange={(v) => set({ avgVolMin: v })}
        />
        <NumRange
          label="MCAP"
          unit="$B"
          min={f.mcapMin}
          max={f.mcapMax}
          onMin={(v) => set({ mcapMin: v })}
          onMax={(v) => set({ mcapMax: v })}
        />
        <Sel label="SECTOR" value={f.sector} options={sectors} onChange={(v) => set({ sector: v })} />
        <Sel
          label="INDUSTRY"
          value={f.industry}
          options={industries}
          onChange={(v) => set({ industry: v })}
        />
        <NumMin label="SENT" step={0.1} value={f.sentMin} onChange={(v) => set({ sentMin: v })} />
        <NumMin label="HEAT" step={0.5} value={f.heatMin} onChange={(v) => set({ heatMin: v })} />
        <NumMin label="BUZZ-z" step={0.5} value={f.buzzMin} onChange={(v) => set({ buzzMin: v })} />
        <button
          onClick={() => set({ hasSignal: !f.hasSignal })}
          className={`flex items-center gap-1 rounded px-1.5 py-0.5 border text-[9.5px] font-semibold tracking-[0.06em] ${
            f.hasSignal
              ? "border-tape-accent text-tape-accent bg-[rgba(79,209,197,0.06)]"
              : "border-tape-border text-tape-faint hover:text-tape-sub"
          }`}
          title="only rows with a directional signal (bullish/bearish)"
        >
          {f.hasSignal ? "☑" : "☐"} HAS SIGNAL
        </button>
        <button
          onClick={onClear}
          disabled={active === 0}
          className="rounded px-2 py-0.5 border border-tape-border text-[9.5px] font-semibold tracking-[0.06em] text-tape-faint hover:text-tape-bear hover:border-tape-bear disabled:opacity-40 disabled:hover:text-tape-faint disabled:hover:border-tape-border"
          title="clear all filters"
        >
          CLEAR ALL
        </button>
      </div>

      {/* status row: count + active filters + live badge + search + density */}
      <div className="flex flex-wrap items-center gap-2.5 tape-mono">
        <span className="text-[11px] font-medium text-tape-muted tabular-nums">
          <span className="text-tape-text font-bold">{matched}</span> / {total.toLocaleString()}
          {active > 0 && (
            <span className="text-tape-accent ml-2">{active} filter{active > 1 ? "s" : ""}</span>
          )}
        </span>
        <span
          className={`text-[9.5px] font-semibold tracking-[0.1em] rounded px-2 py-[3px] border ${
            live
              ? "text-tape-bull border-[#1C3A2C] bg-[rgba(52,211,153,0.06)]"
              : "text-tape-warn border-[#3A2F16] bg-[rgba(240,180,74,0.06)]"
          }`}
          title={
            live
              ? "LIVE: mentions/sentiment/heat + BUZZ-z from /screener/rows; SIGNAL from /predictions; PRICE/%chg/vol from the bar cache; market cap / avg vol / sector / industry from the fundamentals overlay."
              : "Prediction API unreachable — no rows."
          }
        >
          {live ? "● LIVE" : "● OFFLINE"}
        </span>
        {searchSlot && <span className="text-[11px]">{searchSlot}</span>}
        <button
          onClick={onToggleDensity}
          className="ml-auto text-[10.5px] font-medium text-tape-muted border border-tape-border rounded-md px-2.5 py-[4px] hover:text-tape-sub transition-colors"
        >
          density: {density} ▾
        </button>
      </div>
    </div>
  );
}
