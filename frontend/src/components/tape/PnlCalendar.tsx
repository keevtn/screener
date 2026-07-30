"use client";

import { useMemo } from "react";
import type { CalendarResult } from "@/lib/trader";
import { fmtSignedUsd } from "@/lib/trader";

/**
 * Month grid colored by daily realized P&L (Phase 2). Green/red intensity scales
 * with the day's |realized P&L| relative to the month's max, so the shape of a
 * month reads at a glance. Click a day to load its detail. A fresh account with
 * no trades renders a blank-but-intentional grid.
 */

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export default function PnlCalendar({
  monthDate,
  data,
  selected,
  onSelect,
  onPrev,
  onNext,
}: {
  monthDate: Date; // any date within the displayed month
  data: CalendarResult | null;
  selected: string | null;
  onSelect: (date: string) => void;
  onPrev: () => void;
  onNext: () => void;
}) {
  const year = monthDate.getFullYear();
  const month = monthDate.getMonth();
  const days = data?.days ?? {};

  const { cells, maxAbs, monthTotal } = useMemo(() => {
    const first = new Date(year, month, 1);
    const startPad = first.getDay(); // 0=Sun
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const list: (string | null)[] = [];
    for (let i = 0; i < startPad; i++) list.push(null);
    for (let d = 1; d <= daysInMonth; d++) list.push(ymd(new Date(year, month, d)));
    while (list.length % 7 !== 0) list.push(null);
    let mx = 0;
    let total = 0;
    for (const iso of list) {
      if (!iso) continue;
      const c = days[iso];
      if (c) {
        mx = Math.max(mx, Math.abs(c.realized_pl));
        total += c.realized_pl;
      }
    }
    return { cells: list, maxAbs: mx, monthTotal: total };
  }, [year, month, days]);

  const monthLabel = new Date(year, month, 1).toLocaleDateString("en-US", {
    month: "long",
    year: "numeric",
  });

  function bg(pl: number): string {
    if (maxAbs === 0) return "transparent";
    const a = 0.12 + 0.5 * Math.min(1, Math.abs(pl) / maxAbs);
    return pl >= 0 ? `rgba(52,211,153,${a.toFixed(3)})` : `rgba(251,113,133,${a.toFixed(3)})`;
  }

  return (
    <div className="px-[22px] py-3">
      <div className="flex items-center gap-3 mb-3 tape-mono text-[11px]">
        <button
          onClick={onPrev}
          aria-label="Previous month"
          className="px-2 py-1 rounded border border-tape-border text-tape-sub hover:text-tape-accent"
        >
          ←
        </button>
        <span className="text-tape-text font-semibold tracking-[0.08em] min-w-[10rem] text-center">
          {monthLabel}
        </span>
        <button
          onClick={onNext}
          aria-label="Next month"
          className="px-2 py-1 rounded border border-tape-border text-tape-sub hover:text-tape-accent"
        >
          →
        </button>
        <span className="ml-auto text-tape-faint">
          month realized{" "}
          <span className={monthTotal >= 0 ? "text-tape-bull" : "text-tape-bear"}>
            {fmtSignedUsd(monthTotal)}
          </span>
        </span>
      </div>

      <div className="grid grid-cols-7 gap-1" role="grid" aria-label={`P&L calendar for ${monthLabel}`}>
        {WEEKDAYS.map((w) => (
          <div key={w} className="tape-mono text-[9.5px] text-tape-muted uppercase tracking-[0.1em] text-center pb-1">
            {w}
          </div>
        ))}
        {cells.map((iso, i) => {
          if (!iso) return <div key={`pad-${i}`} aria-hidden />;
          const c = days[iso];
          const dayNum = Number(iso.slice(8, 10));
          const isSel = selected === iso;
          return (
            <button
              key={iso}
              onClick={() => onSelect(iso)}
              aria-pressed={isSel}
              aria-label={
                c
                  ? `${iso}: realized ${fmtSignedUsd(c.realized_pl)}, ${c.trips} round-trips`
                  : `${iso}: no trades`
              }
              className={`flex flex-col items-start justify-between h-[52px] p-1.5 rounded border tape-mono text-left transition-colors ${
                isSel ? "border-tape-accent" : "border-tape-border-soft hover:border-tape-border"
              }`}
              style={{ background: c ? bg(c.realized_pl) : "transparent" }}
            >
              <span className="text-[10px] text-tape-faint tabular-nums">{dayNum}</span>
              {c && (
                <span
                  className={`text-[10px] font-semibold tabular-nums ${
                    c.realized_pl >= 0 ? "text-tape-bull" : "text-tape-bear"
                  }`}
                >
                  {fmtSignedUsd(c.realized_pl, 0)}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
