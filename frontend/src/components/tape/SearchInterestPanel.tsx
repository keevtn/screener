"use client";

import type { SearchHourlyPoint } from "@/lib/ticker";

/**
 * Hourly Google-Trends search-interest curve for one ticker — the search-attention
 * mirror of the intraday message-density panel. Bars are Google's OWN-TERM relative
 * index (0-100), NOT a count of searches: Trends never exposes absolute volumes, so
 * every value is scaled to this ticker's own trailing-window peak and is comparable
 * only against itself over time. The header/label/source readout lives in the HTML
 * (see TickerDetailBody), not in the SVG; this is bars + axis only.
 */

export interface SearchBucket {
  hour: number; // epoch ms at hour start
  value: number; // own-term relative interest, 0-100
}

/** Points from the API ({hour: UTC ISO, value}) -> chart buckets, dropping any
 * unparseable timestamp. Trends hourly points are contiguous, so no gap-filling. */
export function searchBuckets(points: SearchHourlyPoint[]): SearchBucket[] {
  const out: SearchBucket[] = [];
  for (const p of points) {
    const t = Date.parse(p.hour);
    if (Number.isNaN(t)) continue;
    out.push({ hour: t, value: typeof p.value === "number" ? p.value : 0 });
  }
  return out.sort((a, b) => a.hour - b.hour);
}

// Geometry mirrors IntradayDensity so the two stacked panels read as one family.
const W = 900;
const H = 104;
const L = 38;
const R = 14;
const TOP = 12; // bar ceiling
const BOT = 84; // baseline
const LABEL_Y = 98; // hour tick row
const C = {
  grid: "#232a38",
  divider: "#171d28",
  axis: "#5a6478",
  bar: "#a78bfa", // violet — distinct from the density panel's teal/slate
  dim: "#5a6478",
};

function fmtHour(ms: number): string {
  return `${String(new Date(ms).getHours()).padStart(2, "0")}:00`;
}
function fmtFull(ms: number): string {
  const d = new Date(ms);
  const day = d.toLocaleDateString(undefined, { weekday: "short" });
  return `${day} ${fmtHour(ms)}`;
}

export default function SearchInterestPanel({ buckets }: { buckets: SearchBucket[] }) {
  if (buckets.length === 0) {
    return (
      <div className="tape-mono text-[10.5px] text-tape-dim px-2 py-6 text-center">
        no hourly search-interest available for this ticker (Google Trends returned nothing —
        never faked)
      </div>
    );
  }
  const n = buckets.length;
  const slot = (W - L - R) / n;
  const bw = Math.max(1.5, slot - 1);
  const cx = (i: number) => L + i * slot + slot / 2;
  // Values are already a 0-100 index; scale to the visible peak (which may be < 100
  // since we slice the tail of the trailing window) and label the true peak.
  const vMax = Math.max(1, ...buckets.map((b) => b.value));
  const vh = (v: number) => (v / vMax) * (BOT - TOP);

  const step = Math.max(1, Math.round((n - 1) / 6));
  const tickIdx: number[] = [];
  for (let i = 0; i < n; i += step) tickIdx.push(i);
  if (tickIdx[tickIdx.length - 1] !== n - 1) tickIdx.push(n - 1);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 128 }}>
      {buckets.map((b, i) =>
        new Date(b.hour).getHours() === 0 && i > 0 && i < n - 1 ? (
          <line key={`div-${i}`} x1={cx(i)} y1={TOP} x2={cx(i)} y2={BOT} stroke={C.divider} />
        ) : null,
      )}
      <line x1={L} y1={BOT} x2={W - R} y2={BOT} stroke={C.grid} />
      <text x={L - 5} y={TOP + 7} textAnchor="end" fontSize="8.5" fill={C.axis} className="tape-mono">
        {Math.round(vMax)}
      </text>
      <text x={L - 5} y={BOT} textAnchor="end" fontSize="8.5" fill={C.dim} className="tape-mono">
        0
      </text>

      {buckets.map((b, i) => {
        const h = vh(b.value);
        return (
          <g key={b.hour}>
            <title>{`${fmtFull(b.hour)} — interest ${Math.round(b.value)}/100 (own-term)`}</title>
            <rect x={cx(i) - bw / 2} y={BOT - h} width={bw} height={h} fill={C.bar} />
          </g>
        );
      })}

      {tickIdx.map((i, k) => (
        <text
          key={`ht-${i}`}
          x={Math.max(L, Math.min(W - R, cx(i)))}
          y={LABEL_Y}
          textAnchor={k === 0 ? "start" : k === tickIdx.length - 1 ? "end" : "middle"}
          fontSize="8.5"
          fill={C.axis}
          className="tape-mono"
        >
          {fmtHour(buckets[i].hour)}
        </text>
      ))}
    </svg>
  );
}
