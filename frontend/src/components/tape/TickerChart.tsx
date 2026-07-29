"use client";

import type { AttentionPoint } from "@/lib/ticker";
import { attnSentiment } from "@/lib/buzz";

/**
 * Attention chart (SVG) with real numeric axes: daily news-volume bars
 * (structured + social) with a buzz-z overlay on a labelled z-axis, and a
 * sentiment line on a labelled -1..+1 axis, sharing a dated x-axis. Price lives
 * in the separate lightweight-charts PriceChart.
 */

const W = 900;
const H = 260;
const L = 46;
const R = 12;
const IW = W - L - R;

// panels
const V_TOP = 16;
const V_BOT = 150; // news volume + buzz-z
const S_TOP = 176;
const S_BOT = 236; // sentiment

const C = {
  grid: "#161a24",
  axis: "#5a6478",
  struct: "#3e4656",
  social: "#4fd1c5",
  buzz: "#f0b44a",
  bull: "#34d399",
  bear: "#fb7185",
  isent: "#7aa2ff", // daily attention-weighted sentiment (matches unified chart)
  text: "#b9c0cf",
};

function ts(d: string): number {
  return new Date(d + "T00:00:00Z").getTime();
}

export default function TickerChart({ attention }: { attention: AttentionPoint[] }) {
  if (attention.length === 0) {
    return (
      <div className="tape-mono text-[11px] text-tape-muted px-4 py-8 text-center">
        no news-attention history for this ticker.
      </div>
    );
  }
  const tsAll = attention.map((a) => ts(a.date));
  const t0 = Math.min(...tsAll);
  const t1 = Math.max(...tsAll);
  const x = (d: string) => L + (t1 === t0 ? IW / 2 : ((ts(d) - t0) / (t1 - t0)) * IW);

  const vMax = Math.max(1, ...attention.map((a) => a.struct + a.social));
  const vh = (n: number) => (n / vMax) * (V_BOT - V_TOP);
  const barW = Math.max(1.5, Math.min(9, IW / attention.length - 1));

  // buzz-z on a fixed z-axis (-1..+5) so the scale is legible
  const zMin = -1;
  const zMax = 5;
  const zy = (z: number) => V_BOT - ((Math.max(zMin, Math.min(zMax, z)) - zMin) / (zMax - zMin)) * (V_BOT - V_TOP);
  const buzzPts = attention.filter((a) => a.buzz_z != null);
  const buzzLine = buzzPts.map((a) => `${x(a.date).toFixed(1)},${zy(a.buzz_z as number).toFixed(1)}`).join(" ");

  // sentiment on -1..+1
  const sy = (s: number) => S_BOT - ((s + 1) / 2) * (S_BOT - S_TOP);
  const sentPts = attention.filter((a) => a.sentiment != null);
  const sentLine = sentPts.map((a) => `${x(a.date).toFixed(1)},${sy(a.sentiment as number).toFixed(1)}`).join(" ");
  // iSent daily: the day's mean sentiment shrunk by its total attention
  // (struct+social mentions) — same formula/K as the unified chart's hourly line.
  const isentLine = sentPts
    .map((a) => `${x(a.date).toFixed(1)},${sy(attnSentiment(a.sentiment as number, a.struct + a.social)).toFixed(1)}`)
    .join(" ");

  const dateIdx = [0, Math.floor(attention.length / 2), attention.length - 1];

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 280 }}>
      {/* volume panel axes */}
      <line x1={L} y1={V_TOP} x2={L} y2={V_BOT} stroke={C.grid} />
      <line x1={L} y1={V_BOT} x2={W - R} y2={V_BOT} stroke={C.grid} />
      <text x={L + 2} y={V_TOP + 8} fontSize="9" fill={C.text} className="tape-mono">
        NEWS VOL (msgs/day) · buzz-z ▬
      </text>
      {/* volume y ticks */}
      {[0, Math.round(vMax / 2), vMax].map((v) => (
        <text key={`v${v}`} x={L - 5} y={V_BOT - vh(v) + 3} textAnchor="end" fontSize="8" fill={C.axis} className="tape-mono">
          {v}
        </text>
      ))}
      {/* buzz z ticks on the right */}
      {[0, 2, 5].map((z) => (
        <g key={`z${z}`}>
          <line x1={L} y1={zy(z)} x2={W - R} y2={zy(z)} stroke={C.grid} strokeDasharray="2 4" />
          <text x={W - R + 2} y={zy(z) + 3} fontSize="8" fill={C.buzz} className="tape-mono">
            {z > 0 ? `+${z}z` : "0z"}
          </text>
        </g>
      ))}
      {attention.map((a) => {
        const cx = x(a.date);
        const hs = vh(a.struct);
        const hsoc = vh(a.social);
        return (
          <g key={a.date}>
            <rect x={cx - barW / 2} y={V_BOT - hs} width={barW} height={hs} fill={C.struct} />
            <rect x={cx - barW / 2} y={V_BOT - hs - hsoc} width={barW} height={hsoc} fill={C.social} />
          </g>
        );
      })}
      {buzzLine && <polyline points={buzzLine} fill="none" stroke={C.buzz} strokeWidth="1.2" />}

      {/* sentiment panel */}
      <line x1={L} y1={S_TOP} x2={L} y2={S_BOT} stroke={C.grid} />
      <text x={L + 2} y={S_TOP - 2} fontSize="9" fill={C.text} className="tape-mono">
        MEAN SENTIMENT (−1..+1)
        <tspan fill={C.social}> — mean</tspan>
        <tspan fill={C.isent}> — iSent (×attn)</tspan>
      </text>
      {[-1, 0, 1].map((s) => (
        <g key={`s${s}`}>
          <line x1={L} y1={sy(s)} x2={W - R} y2={sy(s)} stroke={C.grid} strokeDasharray={s === 0 ? "0" : "2 4"} />
          <text x={L - 5} y={sy(s) + 3} textAnchor="end" fontSize="8" fill={s > 0 ? C.bull : s < 0 ? C.bear : C.axis} className="tape-mono">
            {s > 0 ? "+1" : s}
          </text>
        </g>
      ))}
      {sentLine && <polyline points={sentLine} fill="none" stroke={C.social} strokeWidth="1.2" />}
      {isentLine && <polyline points={isentLine} fill="none" stroke={C.isent} strokeWidth="1.6" />}
      {sentPts.map((a) => (
        <circle
          key={a.date}
          cx={x(a.date)}
          cy={sy(a.sentiment as number)}
          r="1.6"
          fill={(a.sentiment as number) > 0.05 ? C.bull : (a.sentiment as number) < -0.05 ? C.bear : C.axis}
        />
      ))}

      {/* shared x date axis */}
      {dateIdx.map((i, k) => (
        <text
          key={`xdate-${k}`}
          x={Math.max(L, Math.min(W - R, x(attention[i].date)))}
          y={H - 3}
          textAnchor={k === 0 ? "start" : k === dateIdx.length - 1 ? "end" : "middle"}
          fontSize="9"
          fill={C.axis}
          className="tape-mono"
        >
          {attention[i].date}
        </text>
      ))}
    </svg>
  );
}
