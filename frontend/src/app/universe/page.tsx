"use client";

import { useEffect, useMemo, useState } from "react";
import { TickerButton } from "@/components/tape/TickerModalProvider";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import HealthStrip from "@/components/tape/HealthStrip";
import UniverseFilterBar, {
  type ActiveUniFilter,
  type UniOp,
  type UniverseField,
} from "@/components/tape/UniverseFilterBar";
import {
  fetchUniverseFacets,
  fetchUniverseScreen,
  fmtMcap,
  fmtPct,
  fmtVol,
  type UniverseFacets,
  type UniverseFilters,
  type UniverseResult,
} from "@/lib/universe";

/** Format a value only if it's really a finite number; else honest "—". Guards
 *  against a non-numeric string arriving from a REAL DB column (SQLite is
 *  dynamically typed) — one bad row must never crash the panel via toFixed. */
function fin(v: unknown, dp: number, suffix = "", plus = false): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return `${plus && v >= 0 ? "+" : ""}${v.toFixed(dp)}${suffix}`;
}

/**
 * TAPE_ UNIVERSE — the Finviz-style whole-market screener.
 *
 * Filters run server-side over the daily fundamentals snapshot (Finviz Elite);
 * our own signal + next-earnings are overlaid. Distinct from the news-driven
 * SCREENER (which only shows tickers currently in the news window). Filters are
 * composable chips (sector AND cap AND price AND short AND has-signal AND
 * earnings-window …) that all interjoin into one server query; sort is separate.
 */

const PAGE = 50;

const SORTS = [
  ["market_cap", "Mkt Cap"],
  ["change_pct", "%Chg"],
  ["price", "Price"],
  ["avg_volume", "Volume"],
  ["short_float", "Short"],
  ["beta", "Beta"],
  ["ticker", "Ticker"],
] as const;

// Default value seeded when a range/day chip is added (display units).
const RANGE_DEFAULTS: Record<string, number> = {
  mcap: 1,
  price: 5,
  avgvol: 1,
  change: 0,
  short: 5,
  inst: 50,
  insider: 5,
  beta: 1,
  earnings: 7,
};

let _uid = 0;

/** Map the active chips (display units) to the server's /universe/screen params
 * (mcap $B→$M, volume M→thousands, percents →fractions). All chips AND-combine. */
function toServerFilters(
  active: ActiveUniFilter[],
  sort: string,
  order: string,
  offset: number,
): UniverseFilters {
  const f: UniverseFilters = { sort, order, limit: PAGE, offset };
  for (const a of active) {
    const n = Number(a.value);
    switch (a.field) {
      case "mcap":
        if (a.op === "min") f.mcap_min = n * 1000;
        else f.mcap_max = n * 1000;
        break;
      case "price":
        if (a.op === "min") f.price_min = n;
        else f.price_max = n;
        break;
      case "avgvol":
        f.avgvol_min = n * 1000;
        break;
      case "change":
        if (a.op === "min") f.change_min = n / 100;
        else f.change_max = n / 100;
        break;
      case "short":
        if (a.op === "min") f.short_min = n / 100;
        else f.short_max = n / 100;
        break;
      case "inst":
        f.inst_min = n / 100;
        break;
      case "insider":
        f.insider_min = n / 100;
        break;
      case "beta":
        if (a.op === "min") f.beta_min = n;
        else f.beta_max = n;
        break;
      case "sector":
        if (a.value) f.sector = String(a.value);
        break;
      case "industry":
        if (a.value) f.industry = String(a.value);
        break;
      case "signal":
        f.has_signal = true;
        break;
      case "earnings":
        f.earnings_within = n;
        break;
    }
  }
  return f;
}

export default function UniversePage() {
  const clock = useClock();
  const [facets, setFacets] = useState<UniverseFacets | null>(null);
  const [active, setActive] = useState<ActiveUniFilter[]>([]);
  const [sort, setSort] = useState<string>("market_cap");
  const [order, setOrder] = useState<"desc" | "asc">("desc");
  const [offset, setOffset] = useState(0);
  // Ticker/name search — server-side over the whole snapshot (any ticker findable
  // regardless of active filters). qInput is live keystrokes; q is debounced.
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setQ(qInput.trim()), 300);
    return () => clearTimeout(t);
  }, [qInput]);

  const [res, setRes] = useState<UniverseResult>({
    reachable: true,
    count: 0,
    limit: PAGE,
    offset: 0,
    items: [],
  });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchUniverseFacets().then(setFacets);
  }, []);

  // Field vocabulary. Range fields whose server side only has a lower bound
  // (avg vol / inst / insider) offer just ≥. Categorical options come from facets.
  const FIELDS: UniverseField[] = useMemo(
    () => [
      { key: "mcap", label: "mkt cap", kind: "range", unit: "$B", step: 1, ops: ["min", "max"] },
      { key: "price", label: "price", kind: "range", unit: "$", step: 1, ops: ["min", "max"] },
      { key: "avgvol", label: "avg vol", kind: "range", unit: "M", step: 0.5, ops: ["min"] },
      { key: "change", label: "%chg", kind: "range", unit: "%", step: 0.5, ops: ["min", "max"] },
      { key: "short", label: "short", kind: "range", unit: "%", step: 1, ops: ["min", "max"] },
      { key: "inst", label: "inst own", kind: "range", unit: "%", step: 5, ops: ["min"] },
      { key: "insider", label: "insider", kind: "range", unit: "%", step: 1, ops: ["min"] },
      { key: "beta", label: "beta", kind: "range", step: 0.1, ops: ["min", "max"] },
      { key: "sector", label: "sector", kind: "cat", options: facets?.sectors.map((s) => s.name) ?? [] },
      { key: "industry", label: "industry", kind: "cat", options: facets?.industries ?? [] },
      { key: "signal", label: "has signal", kind: "bool" },
      { key: "earnings", label: "earnings", kind: "days", unit: "d" },
    ],
    [facets],
  );

  const filters: UniverseFilters = useMemo(() => {
    const f = toServerFilters(active, sort, order, offset);
    if (q) f.q = q; // ANDs with the chips server-side
    return f;
  }, [active, sort, order, offset, q]);

  // Reset to first page whenever the filter set, sort, or search changes.
  useEffect(() => {
    setOffset(0);
  }, [active, sort, order, q]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchUniverseScreen(filters).then((r) => {
      if (!cancelled) {
        setRes(r);
        setLoading(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [filters]);

  function addFilter(field: string, op: UniOp) {
    setActive((fs) => {
      if (fs.some((f) => f.field === field && f.op === op)) return fs; // no duplicate gate
      const isCat = field === "sector" || field === "industry";
      const value: number | string = isCat ? "" : (RANGE_DEFAULTS[field] ?? 0);
      return [...fs, { id: `${field}-${op}-${_uid++}`, field, op, value }];
    });
  }
  const updateFilter = (id: string, value: number | string) =>
    setActive((fs) => fs.map((f) => (f.id === id ? { ...f, value } : f)));
  const removeFilter = (id: string) => setActive((fs) => fs.filter((f) => f.id !== id));

  const from = res.count === 0 ? 0 : offset + 1;
  const to = Math.min(offset + PAGE, res.count);

  return (
    <>
      <TapeNav active="UNIVERSE" clock={clock} />

      {/* composable filter chips (all interjoin server-side) */}
      <UniverseFilterBar
        fields={FIELDS}
        filters={active}
        matched={res.count}
        total={facets?.universe ?? 0}
        onAdd={addFilter}
        onUpdate={updateFilter}
        onRemove={removeFilter}
        searchSlot={
          <div className="flex items-center gap-1.5">
            <span className="text-tape-faint tracking-[0.1em]">SEARCH</span>
            <div className="relative">
              <input
                value={qInput}
                onChange={(e) => setQInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setQInput("");
                }}
                placeholder="ticker or name…"
                className="bg-tape-panel border border-tape-border rounded pl-2 pr-6 py-1 text-tape-text placeholder-tape-dim focus:border-tape-accent outline-none w-44"
              />
              {qInput && (
                <button
                  onClick={() => setQInput("")}
                  aria-label="clear search"
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 text-tape-faint hover:text-tape-bear leading-none"
                >
                  ×
                </button>
              )}
            </div>
          </div>
        }
      />

      {/* sort control — its own strip */}
      <div className="shrink-0 flex items-center gap-2 px-[22px] py-2 border-b border-tape-border-soft bg-tape-panel tape-mono text-[10.5px]">
        <span className="text-tape-muted tracking-[0.1em] font-semibold">SORT BY</span>
        {SORTS.map(([v, l]) => (
          <button
            key={v}
            onClick={() => setSort(v)}
            className={`px-2 py-0.5 rounded tracking-[0.06em] ${
              sort === v
                ? "bg-tape-panel-2 text-tape-accent border border-tape-border"
                : "text-tape-faint hover:text-tape-sub border border-transparent"
            }`}
          >
            {l}
          </button>
        ))}
        <button
          onClick={() => setOrder((o) => (o === "desc" ? "asc" : "desc"))}
          className="px-1.5 py-0.5 rounded border border-tape-border text-tape-sub hover:border-tape-accent"
          title="toggle sort order"
        >
          {order === "desc" ? "↓" : "↑"}
        </button>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted">
          screening universe…
        </div>
      ) : !res.reachable ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          Prediction API not reachable on :8001 — start it with scripts/serve_api.py.
        </div>
      ) : res.items.length === 0 ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          {!facets?.universe
            ? "No fundamentals snapshot yet — run scripts/snapshot_fundamentals.py."
            : q
            ? `No universe match for "${q}" (symbol prefix or company-name substring).`
            : "No tickers match these filters — widen the screen."}
        </div>
      ) : (
        <div className="flex-1 overflow-auto">
          <table className="w-full border-collapse tape-mono text-[11px] min-w-[1100px]">
            <thead>
              <tr className="sticky top-0 bg-tape-panel-2 text-tape-muted text-left tracking-[0.08em] border-b border-tape-border z-10">
                <th className="px-4 py-2 font-semibold">TICKER</th>
                <th className="px-2 py-2 font-semibold">SECTOR</th>
                <th className="px-2 py-2 font-semibold">INDUSTRY</th>
                <th className="px-2 py-2 font-semibold text-right">MKT CAP</th>
                <th className="px-2 py-2 font-semibold text-right">PRICE</th>
                <th className="px-2 py-2 font-semibold text-right">%CHG</th>
                <th className="px-2 py-2 font-semibold text-right">AVG VOL</th>
                <th className="px-2 py-2 font-semibold text-right">SHORT</th>
                <th className="px-2 py-2 font-semibold text-right">INST</th>
                <th className="px-2 py-2 font-semibold text-right">BETA</th>
                <th className="px-2 py-2 font-semibold">SIGNAL</th>
                <th className="px-3 py-2 font-semibold">EARN</th>
              </tr>
            </thead>
            <tbody>
              {res.items.map((r) => (
                <tr key={r.ticker} className="border-b border-tape-border-soft hover:bg-tape-panel-2">
                  <td className="px-4 py-2">
                    <TickerButton
                      ticker={r.ticker}
                      className="text-tape-text font-semibold hover:text-tape-accent hover:underline"
                    />
                    {r.name && (
                      <span className="text-tape-faint ml-2 truncate">{r.name.slice(0, 22)}</span>
                    )}
                  </td>
                  <td className="px-2 py-2 text-tape-faint">{r.sector ?? "—"}</td>
                  <td className="px-2 py-2 text-tape-dim truncate max-w-[10rem]">
                    {r.industry ?? "—"}
                  </td>
                  <td className="px-2 py-2 text-tape-sub text-right">{fmtMcap(r.market_cap)}</td>
                  <td className="px-2 py-2 text-tape-text text-right">{fin(r.price, 2)}</td>
                  <td
                    className={`px-2 py-2 text-right ${
                      typeof r.change_pct !== "number" || !Number.isFinite(r.change_pct)
                        ? "text-tape-faint"
                        : r.change_pct >= 0
                        ? "text-tape-bull"
                        : "text-tape-bear"
                    }`}
                  >
                    {typeof r.change_pct === "number" && Number.isFinite(r.change_pct)
                      ? fin(r.change_pct * 100, 1, "%", true)
                      : "—"}
                  </td>
                  <td className="px-2 py-2 text-tape-muted text-right">{fmtVol(r.avg_volume)}</td>
                  <td className="px-2 py-2 text-tape-sub text-right">{fmtPct(r.short_float)}</td>
                  <td className="px-2 py-2 text-tape-faint text-right">{fmtPct(r.inst_own)}</td>
                  <td className="px-2 py-2 text-tape-faint text-right">{fin(r.beta, 2)}</td>
                  <td className="px-2 py-2">
                    {r.signal ? (
                      <span
                        className={
                          r.signal.direction === "bullish" ? "text-tape-bull" : "text-tape-bear"
                        }
                      >
                        {r.signal.direction === "bullish" ? "▲" : "▼"}{" "}
                        {fin(r.signal.confidence, 2)}
                      </span>
                    ) : (
                      <span className="text-tape-dim">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-tape-warn">{r.next_earnings ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* pagination + count */}
      <div className="shrink-0 flex justify-between items-center px-[22px] py-2.5 tape-mono text-[10.5px] text-tape-faint border-t border-tape-border-soft">
        <span>
          {from.toLocaleString()}–{to.toLocaleString()} of{" "}
          <span className="text-tape-sub">{res.count.toLocaleString()}</span> matched
          {facets?.universe ? ` · ${facets.universe.toLocaleString()} universe` : ""}
        </span>
        <span className="flex gap-2">
          <button
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE))}
            className="px-2 py-1 rounded border border-tape-border disabled:opacity-30 hover:border-tape-accent"
          >
            ← prev
          </button>
          <button
            disabled={to >= res.count}
            onClick={() => setOffset(offset + PAGE)}
            className="px-2 py-1 rounded border border-tape-border disabled:opacity-30 hover:border-tape-accent"
          >
            next →
          </button>
        </span>
      </div>

      <HealthStrip
        note={
          res.reachable
            ? `universe :8001 · daily Finviz fundamentals${facets?.as_of ? ` (as of ${facets.as_of})` : ""} · signal + earnings overlaid`
            : "prediction API offline :8001"
        }
      />
    </>
  );
}
