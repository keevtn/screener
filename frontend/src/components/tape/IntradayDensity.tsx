"use client";

import type { NewsItem } from "@/types/news";

/**
 * Live intraday message-density curve: hourly buckets of news published_at for a
 * ticker over the last N hours (structured + social stacked). Computed on the
 * client straight from the news window — no storage. Empty slots render as gaps
 * so quiet hours are visible. The title/totals/LIVE readout lives in the HTML
 * header (see TickerDetailBody), NOT in the SVG — a tall bar used to render over
 * an in-SVG header line. The chart is bars + axis only.
 */

export interface HourBucket {
  hour: number; // epoch ms at hour start
  struct: number;
  social: number;
}

export interface DensityTotals {
  h24: { struct: number; social: number };
  h48: { struct: number; social: number };
}

export function hourlyBuckets(items: NewsItem[], hours: number, nowMs: number): HourBucket[] {
  const HR = 3_600_000;
  const start = Math.floor((nowMs - hours * HR) / HR) * HR;
  const end = Math.floor(nowMs / HR) * HR;
  const map = new Map<number, HourBucket>();
  for (const it of items) {
    const t = Date.parse(it.published_at);
    if (Number.isNaN(t) || t < start) continue;
    const hk = Math.floor(t / HR) * HR;
    const b = map.get(hk) ?? { hour: hk, struct: 0, social: 0 };
    if (it.source_type === "social") b.social += 1;
    else b.struct += 1;
    map.set(hk, b);
  }
  const out: HourBucket[] = [];
  for (let h = start; h <= end; h += HR) out.push(map.get(h) ?? { hour: h, struct: 0, social: 0 });
  return out;
}

/** Message counts over the trailing 24h and 48h (struct/social split), computed
 * independently of the panel's window toggle so both totals are always shown. */
export function messageTotals(items: NewsItem[], nowMs: number): DensityTotals {
  const HR = 3_600_000;
  const t24 = nowMs - 24 * HR;
  const t48 = nowMs - 48 * HR;
  const zero = () => ({ struct: 0, social: 0 });
  const r: DensityTotals = { h24: zero(), h48: zero() };
  for (const it of items) {
    const t = Date.parse(it.published_at);
    if (Number.isNaN(t) || t < t48) continue;
    const key = it.source_type === "social" ? "social" : "struct";
    r.h48[key] += 1;
    if (t >= t24) r.h24[key] += 1;
  }
  return r;
}

// Geometry — no in-SVG header band any more, so bars get the full height.
const W = 900;
const H = 104;
const L = 38;
const R = 14;
const TOP = 12; // bar ceiling
const BOT = 84; // baseline
const LABEL_Y = 98; // hour tick row
const C = {
  grid: "#232a38",
  divider: "#171d28", // subtle midnight day-boundary
  axis: "#5a6478",
  struct: "#3e4656",
  social: "#4fd1c5",
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

export default function IntradayDensity({ buckets }: { buckets: HourBucket[] }) {
  const total = buckets.reduce((s, b) => s + b.struct + b.social, 0);
  if (total === 0) {
    return (
      <div className="tape-mono text-[10.5px] text-tape-dim px-2 py-6 text-center">
        no intraday news for this ticker in the current window
      </div>
    );
  }
  const n = buckets.length;
  const slot = (W - L - R) / n;
  const bw = Math.max(1.5, slot - 1);
  const cx = (i: number) => L + i * slot + slot / 2;
  const vMax = Math.max(1, ...buckets.map((b) => b.struct + b.social));
  const vh = (v: number) => (v / vMax) * (BOT - TOP);

  // Hour ticks: ~6-7 evenly spaced across the window so labels actually VARY.
  // (The old 3-label start/mid/end sampling of a 48h window showed "12:00"
  // three times — 0h/24h/48h are the same clock hour.)
  const step = Math.max(1, Math.round((n - 1) / 6));
  const tickIdx: number[] = [];
  for (let i = 0; i < n; i += step) tickIdx.push(i);
  if (tickIdx[tickIdx.length - 1] !== n - 1) tickIdx.push(n - 1);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 128 }}>
      {/* midnight day-dividers give the multi-day window temporal anchors */}
      {buckets.map((b, i) =>
        new Date(b.hour).getHours() === 0 && i > 0 && i < n - 1 ? (
          <line key={`div-${i}`} x1={cx(i)} y1={TOP} x2={cx(i)} y2={BOT} stroke={C.divider} />
        ) : null,
      )}
      <line x1={L} y1={BOT} x2={W - R} y2={BOT} stroke={C.grid} />
      {/* y-axis extents in the left gutter (clear of the bars, which start at L) */}
      <text x={L - 5} y={TOP + 7} textAnchor="end" fontSize="8.5" fill={C.axis} className="tape-mono">
        {vMax}
      </text>
      <text x={L - 5} y={BOT} textAnchor="end" fontSize="8.5" fill={C.dim} className="tape-mono">
        0
      </text>

      {buckets.map((b, i) => {
        const hs = vh(b.struct);
        const hsoc = vh(b.social);
        const tot = b.struct + b.social;
        return (
          <g key={b.hour}>
            {tot > 0 && <title>{`${fmtFull(b.hour)} — ${b.struct} news · ${b.social} social`}</title>}
            <rect x={cx(i) - bw / 2} y={BOT - hs} width={bw} height={hs} fill={C.struct} />
            <rect x={cx(i) - bw / 2} y={BOT - hs - hsoc} width={bw} height={hsoc} fill={C.social} />
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
