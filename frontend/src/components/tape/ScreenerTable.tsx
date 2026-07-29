"use client";

import { CSSProperties, useState } from "react";
import { TickerButton } from "@/components/tape/TickerModalProvider";
import { Density, ScreenerRow } from "@/lib/tape/types";
import { C, MONO, signColor } from "@/lib/tape/tokens";
import type { StatsMap, TickerStats } from "@/lib/tape/screenerStats";
import { ratingOf, sentPhrase, type Tone } from "@/lib/tape/insight";
import { fmtMcap, fmtVol } from "@/lib/universe";

/**
 * The screener grid (design 2a): a fixed, pinned ticker column on the left and
 * four column groups — SIGNAL STATE / SENTIMENT·ATTENTION / EVENTS / PRICE —
 * that scroll horizontally as one under a right-edge fade. Left and right blocks
 * render the same rows at identical heights (HEADER_H / rowH) so they stay
 * row-aligned without a shared grid.
 */

const HEADER_H = 56;
const GROUP_COLS = "repeat(3,1fr) 1.55fr"; // 3 equal groups + a wider PRICE (now 5 leaves)

// leaf-column templates per group
const T_SIGNAL = "1.3fr .7fr .8fr";
const T_SENT = ".9fr .9fr .8fr .8fr .8fr .9fr"; // + xNORM (mentions vs own history)
const T_EVENT = ".6fr 1.2fr .8fr";
const T_PRICE = ".8fr .7fr .6fr 1.1fr 1fr"; // LAST %CHG VOL/AVG VOL MCAP
const STRIP_H = 58; // expanded INSIGHT strip height (both sides, keeps row alignment)

const mono = (size: number, weight = 500): CSSProperties => ({
  font: `${weight} ${size}px ${MONO}`,
});

/** border-left + padding for the 2nd..4th groups (matches design). */
function groupCell(index: number, extra: CSSProperties = {}): CSSProperties {
  return index === 0
    ? extra
    : { borderLeft: `1px solid ${C.borderSoft}`, paddingLeft: 14, ...extra };
}

function SignalCell({ row }: { row: ScreenerRow }) {
  const { direction, confidence, config } = row.signal;
  const dir =
    direction === "bullish"
      ? { txt: "▲ BULLISH", color: C.bull }
      : direction === "bearish"
      ? { txt: "▼ BEARISH", color: C.bear }
      : { txt: "— NONE", color: C.faint };
  return (
    <div style={{ display: "grid", gridTemplateColumns: T_SIGNAL, alignItems: "center", ...mono(11.5) }}>
      <span style={{ color: dir.color, fontWeight: 600 }}>{dir.txt}</span>
      <span
        style={{
          color: confidence == null ? C.faint : C.text,
          justifySelf: "start",
          borderBottom: confidence == null ? "none" : `1px dotted ${C.dim}`,
        }}
        title={confidence == null ? undefined : "Click through to evidence (planned)"}
      >
        {confidence == null ? "—" : confidence.toFixed(2).replace(/^0/, "")}
      </span>
      <span style={{ color: config ? C.muted : C.faint }}>{config ?? "—"}</span>
    </div>
  );
}

function AttentionCell({
  row,
  index,
  xNorm,
}: {
  row: ScreenerRow;
  index: number;
  xNorm: number | null;
}) {
  const a = row.attention;
  return (
    <div style={{ display: "grid", gridTemplateColumns: T_SENT, alignItems: "center", ...groupCell(index, mono(11.5)) }}>
      <span style={{ color: signColor(a.sentiment) }}>{a.sentiment.toFixed(2)}</span>
      <span style={{ color: a.heat === 0 ? C.faint : signColor(a.heat), fontWeight: 600 }}>
        {a.heat >= 0 ? "+" : ""}
        {a.heat.toFixed(1)}
      </span>
      <span style={{ color: a.buzzZ == null ? C.faint : C.text, fontWeight: 700 }}>
        {a.buzzZ == null ? "—" : `${a.buzzZ > 0 ? "+" : ""}${a.buzzZ.toFixed(1)}`}
      </span>
      <span style={{ color: C.muted }}>{a.mentions}</span>
      <span style={{ color: C.text }}>{a.authors}</span>
      <span
        style={{
          color: xNorm == null ? C.faint : xNorm >= 2 ? C.warn : C.sub,
          fontWeight: xNorm != null && xNorm >= 2 ? 700 : 500,
        }}
        title="today's rollup mentions vs this ticker's own daily average (n>=5 days)"
      >
        {xNorm == null ? "—" : `${xNorm.toFixed(1)}×`}
      </span>
    </div>
  );
}

/** Semantic tone (lib/tape/insight) mapped onto this table's palette. */
const TONE_COLOR: Record<Tone, string> = { bull: C.bull, bear: C.bear, muted: C.muted };

function InsightStrip({ row, s }: { row: ScreenerRow; s: TickerStats | undefined }) {
  const a = row.attention;
  const rating = ratingOf(a.sentiment);
  const chunk: CSSProperties = { display: "inline-flex", gap: 5, alignItems: "baseline" };
  const label: CSSProperties = { color: C.dim, letterSpacing: ".1em", ...mono(9, 600) };
  return (
    <div
      style={{
        height: STRIP_H,
        display: "flex",
        alignItems: "center",
        gap: 22,
        flexWrap: "wrap",
        rowGap: 4,
        borderBottom: `1px solid ${C.line}`,
        background: "rgba(79,209,197,.03)",
        paddingLeft: 4,
        ...mono(10.5),
      }}
    >
      <span style={chunk}>
        <span style={label}>RATING</span>
        <span style={{ color: TONE_COLOR[rating.tone], fontWeight: 700 }}>{rating.txt}</span>
        <span style={{ color: signColor(a.sentiment) }}>
          {a.sentiment >= 0 ? "+" : ""}
          {a.sentiment.toFixed(2)}
        </span>
        <span style={{ color: C.faint }}>({a.mentions} window mentions · {a.authors} sources)</span>
      </span>
      <span style={chunk}>
        <span style={label}>TODAY VS SELF</span>
        {s && s.mentions_x_normal != null ? (
          <>
            <span style={{ color: s.mentions_x_normal >= 2 ? C.warn : C.text, fontWeight: 700 }}>
              {s.mentions_x_normal.toFixed(1)}× normal
            </span>
            <span style={{ color: C.faint }}>
              ({s.mentions_today} today: {s.struct_today} news + {s.social_today} social · avg{" "}
              {s.avg_daily_mentions?.toFixed(1)}/d over {s.n_days}d)
            </span>
          </>
        ) : (
          <span style={{ color: C.faint }}>
            — insufficient history (n={s?.n_days ?? 0}d, needs ≥5)
          </span>
        )}
      </span>
      <span style={chunk}>
        <span style={label}>SENT VS SELF</span>
        {s && s.sent_z != null ? (
          <>
            <span style={{ color: signColor(s.sent_z), fontWeight: 700 }}>
              z {s.sent_z >= 0 ? "+" : ""}
              {s.sent_z.toFixed(1)}
            </span>
            <span style={{ color: C.sub }}>{sentPhrase(s.sent_z)}</span>
            <span style={{ color: C.faint }}>
              (today {s.sent_today?.toFixed(2)} vs own {s.sent_hist_mean?.toFixed(2)}±
              {s.sent_hist_std?.toFixed(2)})
            </span>
          </>
        ) : (
          <span style={{ color: C.faint }}>—</span>
        )}
      </span>
      <span style={chunk}>
        <span style={label}>BUZZ BASE</span>
        {s?.buzz_baseline ? (
          <span style={{ color: C.sub }}>
            μ {s.buzz_baseline.mean.toFixed(1)}±{s.buzz_baseline.std.toFixed(1)}/d
            <span style={{ color: C.faint }}>
              {" "}
              (n={s.buzz_baseline.n_days} · {s.buzz_baseline.source})
            </span>
          </span>
        ) : (
          <span style={{ color: C.faint }}>—</span>
        )}
      </span>
      <span style={chunk}>
        <span style={label}>LAST CATALYST</span>
        <span style={{ color: row.event.label ? C.sub : C.faint }}>
          {row.event.label ?? "—"}
          {row.event.ageHours != null && (
            <span style={{ color: C.faint }}> · {row.event.ageHours}h ago</span>
          )}
        </span>
      </span>
    </div>
  );
}

function EventCell({ row, index }: { row: ScreenerRow; index: number }) {
  const e = row.event;
  return (
    <div style={{ display: "grid", gridTemplateColumns: T_EVENT, alignItems: "center", ...groupCell(index, mono(11)) }}>
      <span style={{ color: e.material ? C.warn : C.dim }}>●</span>
      <span style={{ color: e.label ? C.sub : C.faint, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {e.label ?? "—"}
      </span>
      <span style={{ color: C.muted }}>{e.ageHours == null ? "—" : `${e.ageHours}h`}</span>
    </div>
  );
}

function PriceCell({ row, index }: { row: ScreenerRow; index: number }) {
  const p = row.price;
  // Numeric VOLUME: prefer today's (= avg volume × relative-volume ratio, both from
  // the fundamentals + quote overlays), else fall back to avg volume, labelled "avg"
  // (raw intraday volume isn't carried in the row model). avgVolume is in THOUSANDS.
  const avgK = row.fundamentals?.avgVolume ?? null;
  const todayK = avgK != null && p.volOverAvg != null ? avgK * p.volOverAvg : null;
  const volK = todayK ?? avgK;
  const isAvg = todayK == null && avgK != null;
  return (
    <div style={{ display: "grid", gridTemplateColumns: T_PRICE, alignItems: "center", ...groupCell(index, mono(11.5)) }}>
      <span style={{ color: C.text }}>{p.last == null ? "—" : p.last.toFixed(2)}</span>
      <span style={{ color: p.pctChange == null ? C.faint : signColor(p.pctChange) }}>
        {p.pctChange == null ? "—" : `${p.pctChange > 0 ? "+" : ""}${p.pctChange.toFixed(1)}%`}
      </span>
      <span style={{ color: p.volOverAvg == null ? C.faint : C.muted, ...mono(11) }}>
        {p.volOverAvg == null ? "—" : `${p.volOverAvg.toFixed(1)}×`}
      </span>
      <span
        style={{ color: volK == null ? C.faint : isAvg ? C.muted : C.text, whiteSpace: "nowrap" }}
        title={
          volK == null
            ? "no volume data"
            : isAvg
            ? "average daily volume (raw intraday volume not carried)"
            : "today's volume (avg × relative-volume ratio)"
        }
      >
        {fmtVol(volK)}
        {isAvg && <span style={{ color: C.faint, ...mono(8.5) }}> avg</span>}
      </span>
      <span style={{ color: row.fundamentals?.marketCap == null ? C.faint : C.text }}>
        {fmtMcap(row.fundamentals?.marketCap ?? null)}
      </span>
    </div>
  );
}

const GROUPS = [
  { label: "SIGNAL STATE", tpl: T_SIGNAL, cols: ["DIR", "CONF ▾", "CFG"] },
  { label: "SENTIMENT / ATTENTION", tpl: T_SENT, cols: ["SENT", "HEAT", "BUZZ z", "MENT", "AUTH", "×NORM"] },
  { label: "EVENTS", tpl: T_EVENT, cols: ["MAT", "LAST EVENT", "AGE"] },
  { label: "PRICE", tpl: T_PRICE, cols: ["LAST", "%CHG", "VOL/AVG", "VOL", "MKT CAP"] },
];

export default function ScreenerTable({
  rows,
  density,
  stats = {},
}: {
  rows: ScreenerRow[];
  density: Density;
  stats?: StatsMap;
}) {
  const rowH = density === "comfortable" ? 52 : 41;
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (t: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });

  const rowHighlight = (r: ScreenerRow): boolean =>
    r.event.material && r.event.ageHours != null && r.event.ageHours < 1;

  return (
    <div style={{ position: "relative", flex: 1, paddingLeft: 22, minHeight: 0, overflowY: "auto", overflowX: "hidden" }}>
      <div style={{ display: "grid", gridTemplateColumns: "132px minmax(0,1fr)" }}>
        {/* ── Pinned ticker column (outside the horizontal scroller) ── */}
        <div style={{ borderRight: `1px solid ${C.border}`, background: C.panel }}>
          <div
            style={{
              height: HEADER_H,
              display: "flex",
              alignItems: "flex-end",
              paddingBottom: 9,
              borderBottom: `1px solid ${C.border}`,
              color: C.faint,
              letterSpacing: ".08em",
              ...mono(9.5, 600),
            }}
          >
            TICKER ⊙
          </div>
          {rows.map((r) => (
            <div key={r.ticker}>
              <div
                style={{
                  height: rowH,
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  borderBottom: `1px solid ${C.line}`,
                  background: rowHighlight(r) ? "rgba(240,180,74,.025)" : "transparent",
                }}
              >
                <button
                  onClick={() => toggle(r.ticker)}
                  title="insight strip: live stats vs this ticker's own history"
                  style={{
                    color: open.has(r.ticker) ? C.accent : C.dim,
                    background: "none",
                    border: "none",
                    cursor: "pointer",
                    padding: 0,
                    width: 12,
                    ...mono(10, 700),
                  }}
                >
                  {open.has(r.ticker) ? "▾" : "▸"}
                </button>
                <TickerButton
                  ticker={r.ticker}
                  className="hover:underline"
                  style={{ color: C.text, ...mono(12, 700) }}
                />
                {r.name !== r.ticker && (
                  <span style={{ color: C.faint, font: `400 9.5px 'IBM Plex Sans',sans-serif` }}>{r.name}</span>
                )}
              </div>
              {open.has(r.ticker) && (
                <div
                  style={{
                    height: STRIP_H,
                    borderBottom: `1px solid ${C.line}`,
                    display: "flex",
                    alignItems: "center",
                    color: C.dim,
                    ...mono(9, 600),
                  }}
                >
                  INSIGHT
                </div>
              )}
            </div>
          ))}
        </div>

        {/* ── Horizontally-scrolling column groups ── */}
        <div style={{ minWidth: 0, position: "relative" }}>
          <div className="tape-xscroll" style={{ overflowX: "auto" }}>
          <div style={{ minWidth: 1520, paddingLeft: 16 }}>
            {/* header: group labels + leaf column names, together HEADER_H tall */}
            <div style={{ height: HEADER_H, borderBottom: `1px solid ${C.border}`, display: "flex", flexDirection: "column", justifyContent: "flex-end" }}>
              <div style={{ display: "grid", gridTemplateColumns: GROUP_COLS }}>
                {GROUPS.map((g, i) => (
                  <div
                    key={g.label}
                    style={{
                      color: i === 0 ? C.accent : C.dim,
                      letterSpacing: ".16em",
                      padding: "0 0 2px",
                      ...groupCell(i, mono(9, 600)),
                    }}
                  >
                    {g.label}
                  </div>
                ))}
              </div>
              <div style={{ display: "grid", gridTemplateColumns: GROUP_COLS }}>
                {GROUPS.map((g, i) => (
                  <div
                    key={g.label}
                    style={{ display: "grid", gridTemplateColumns: g.tpl, color: C.faint, letterSpacing: ".08em", padding: "7px 0 9px", ...groupCell(i, mono(9.5, 600)) }}
                  >
                    {g.cols.map((c) => (
                      <span key={c}>{c}</span>
                    ))}
                  </div>
                ))}
              </div>
            </div>

            {/* data rows */}
            {rows.map((r) => (
              <div key={r.ticker}>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: GROUP_COLS,
                    alignItems: "center",
                    height: rowH,
                    borderBottom: `1px solid ${C.line}`,
                    background: rowHighlight(r) ? "rgba(240,180,74,.025)" : "transparent",
                  }}
                >
                  <SignalCell row={r} />
                  <AttentionCell
                    row={r}
                    index={1}
                    xNorm={stats[r.ticker]?.mentions_x_normal ?? null}
                  />
                  <EventCell row={r} index={2} />
                  <PriceCell row={r} index={3} />
                </div>
                {open.has(r.ticker) && <InsightStrip row={r} s={stats[r.ticker]} />}
              </div>
            ))}
          </div>
          </div>

          {/* right-edge fade + scroll affordance (anchored to the visible edge) */}
          <div
            style={{
              position: "absolute",
              top: 0,
              right: 0,
              bottom: 0,
              width: 70,
              background: `linear-gradient(90deg,rgba(11,13,18,0),${C.panel})`,
              display: "flex",
              alignItems: "center",
              justifyContent: "flex-end",
              paddingRight: 10,
              pointerEvents: "none",
            }}
          >
            <span style={{ color: C.muted, background: "#12161F", border: `1px solid ${C.border}`, borderRadius: 10, padding: "4px 9px", ...mono(10, 600) }}>
              scroll →
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
