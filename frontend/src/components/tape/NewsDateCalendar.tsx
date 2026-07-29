"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { NewsDateOption } from "@/lib/newsArchive";

/**
 * Date picker for the LIVE tape's archive mode — a compact month-grid popover over
 * the days that actually have news (newest first). Default is LIVE (the rolling
 * tape); picking a past day switches the tape to that day's archive. Days with no
 * ingested news are dimmed and unclickable; the calendar never invents a day.
 *
 * Mirrors the movers-band date selector's role, as a calendar because news lands
 * nearly every day and a month grid reads faster than a long dropdown.
 */

function parseISO(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}
function toISO(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}
function monthKey(d: Date): number {
  return d.getFullYear() * 12 + d.getMonth();
}

const WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"];

export default function NewsDateCalendar({
  dates,
  selected,
  onSelect,
}: {
  dates: NewsDateOption[];
  selected: string | null; // ISO date, or null = LIVE
  onSelect: (date: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  const available = useMemo(() => new Set(dates.map((d) => d.date)), [dates]);
  const labelOf = useMemo(
    () => new Map(dates.map((d) => [d.date, d.label])),
    [dates],
  );
  // Range the grid can page through: oldest..newest day that has news.
  const bounds = useMemo(() => {
    if (dates.length === 0) return null;
    return { min: parseISO(dates[dates.length - 1].date), max: parseISO(dates[0].date) };
  }, [dates]);

  // Month currently shown in the grid; defaults to the selected day's month (or latest).
  const [view, setView] = useState<Date>(() =>
    parseISO(selected ?? dates[0]?.date ?? toISO(new Date())),
  );
  useEffect(() => {
    if (selected) setView(parseISO(selected));
    else if (dates[0]) setView(parseISO(dates[0].date));
  }, [selected, dates]);

  // Close on outside-click / Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const todayISO = toISO(new Date());
  const viewStart = new Date(view.getFullYear(), view.getMonth(), 1);
  const daysInMonth = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
  const lead = viewStart.getDay(); // blank cells before day 1
  const monthTitle = viewStart.toLocaleString("en-US", { month: "long", year: "numeric" });

  const canPrev = bounds && monthKey(viewStart) > monthKey(bounds.min);
  const canNext = bounds && monthKey(viewStart) < monthKey(bounds.max);
  const stepMonth = (delta: number) =>
    setView(new Date(view.getFullYear(), view.getMonth() + delta, 1));

  const triggerLabel = selected ? labelOf.get(selected) ?? selected : "LIVE";

  const cells: (number | null)[] = [
    ...Array(lead).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];

  return (
    <div ref={wrapRef} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className={`flex items-center gap-1.5 px-2.5 py-1 rounded border tracking-[0.08em] ${
          selected
            ? "border-tape-border bg-tape-panel text-tape-warn"
            : "border-tape-border bg-tape-panel text-tape-accent"
        }`}
        title="browse a past day's newsfeed"
        aria-label="newsfeed date"
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <span aria-hidden>▦</span>
        {triggerLabel}
        <span aria-hidden className="text-tape-faint">
          ▾
        </span>
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="pick a newsfeed date"
          className="absolute left-0 top-[calc(100%+6px)] z-30 w-[230px] rounded border border-tape-border bg-tape-panel-2 p-2 shadow-lg tape-mono"
        >
          <div className="flex items-center justify-between mb-2">
            <button
              onClick={() => canPrev && stepMonth(-1)}
              disabled={!canPrev}
              className="px-1.5 text-tape-sub disabled:text-tape-faint disabled:cursor-default hover:text-tape-accent"
              aria-label="previous month"
            >
              ‹
            </button>
            <span className="text-[10.5px] tracking-[0.08em] text-tape-sub font-semibold">
              {monthTitle}
            </span>
            <button
              onClick={() => canNext && stepMonth(1)}
              disabled={!canNext}
              className="px-1.5 text-tape-sub disabled:text-tape-faint disabled:cursor-default hover:text-tape-accent"
              aria-label="next month"
            >
              ›
            </button>
          </div>

          <div className="grid grid-cols-7 gap-0.5 text-center">
            {WEEKDAYS.map((w, i) => (
              <div key={i} className="text-[8.5px] text-tape-faint py-0.5">
                {w}
              </div>
            ))}
            {cells.map((day, i) => {
              if (day == null) return <div key={`b${i}`} />;
              const iso = toISO(new Date(view.getFullYear(), view.getMonth(), day));
              const has = available.has(iso);
              const isSel = iso === selected;
              const isToday = iso === todayISO;
              return (
                <button
                  key={iso}
                  disabled={!has}
                  onClick={() => {
                    onSelect(iso);
                    setOpen(false);
                  }}
                  className={`h-6 rounded text-[10px] leading-6 ${
                    isSel
                      ? "bg-tape-accent text-tape-panel font-semibold"
                      : has
                        ? "text-tape-sub hover:bg-tape-panel"
                        : "text-tape-faint/50 cursor-default"
                  } ${isToday && !isSel ? "ring-1 ring-tape-border" : ""}`}
                  title={has ? iso : "no news"}
                >
                  {day}
                </button>
              );
            })}
          </div>

          {selected && (
            <button
              onClick={() => {
                onSelect(null);
                setOpen(false);
              }}
              className="mt-2 w-full rounded border border-tape-border py-1 text-[10px] tracking-[0.08em] text-tape-accent hover:bg-tape-panel"
            >
              ← BACK TO LIVE
            </button>
          )}
        </div>
      )}
    </div>
  );
}
