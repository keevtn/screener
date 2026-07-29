"use client";

import { useEffect, useMemo, useRef, useState } from "react";

/**
 * TAPE_ filter combobox — a textbox + volume-ranked suggestion dropdown used by
 * the LIVE tape's ticker/headline filters.
 *
 * Suggestions are ordered by frequency within the current news window (most
 * volume first). Typing narrows the dropdown by substring; Enter or a click adds
 * a chip. Selected values render as clearable chips; the parent combines filters.
 */

export interface Suggestion {
  value: string;
  count: number;
  /** Secondary match target + dim label (e.g. company name for a ticker). */
  hint?: string;
}

interface Props {
  label: string;
  placeholder: string;
  /** Full ranked suggestion list (already sorted by volume, desc). */
  suggestions: Suggestion[];
  /** Active chips. */
  selected: string[];
  onAdd: (value: string) => void;
  onRemove: (value: string) => void;
  /** Uppercase the typed/added token (tickers). */
  uppercase?: boolean;
}

const MAX_SHOWN = 8;

export default function TapeFilterCombo({
  label,
  placeholder,
  suggestions,
  selected,
  onAdd,
  onRemove,
  uppercase = false,
}: Props) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Close the dropdown on outside click.
  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  const selectedSet = useMemo(() => new Set(selected), [selected]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return suggestions
      .filter(
        (s) =>
          !selectedSet.has(s.value) &&
          (!q ||
            s.value.toLowerCase().includes(q) ||
            (s.hint ?? "").toLowerCase().includes(q)),
      )
      .slice(0, MAX_SHOWN);
  }, [suggestions, query, selectedSet]);

  function commit(raw: string) {
    const v = uppercase ? raw.trim().toUpperCase() : raw.trim();
    if (!v || selectedSet.has(v)) return;
    onAdd(v);
    setQuery("");
  }

  return (
    <div ref={wrapRef} className="relative flex items-center gap-1.5">
      <span className="text-tape-faint tracking-[0.1em]">{label}</span>
      <div className="flex flex-wrap items-center gap-1">
        {selected.map((v) => (
          <span
            key={v}
            className="flex items-center gap-1 px-1.5 py-0.5 rounded border border-tape-border bg-tape-panel text-tape-accent text-[10px]"
          >
            {v}
            <button
              type="button"
              onClick={() => onRemove(v)}
              aria-label={`clear ${v}`}
              className="text-tape-faint hover:text-tape-bear leading-none"
            >
              ×
            </button>
          </span>
        ))}
        <input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit(shown.length === 1 ? shown[0].value : query);
            } else if (e.key === "Backspace" && !query && selected.length) {
              onRemove(selected[selected.length - 1]);
            } else if (e.key === "Escape") {
              setOpen(false);
            }
          }}
          placeholder={selected.length ? "" : placeholder}
          className="bg-tape-panel border border-tape-border rounded px-2 py-1 text-tape-text placeholder-tape-dim focus:border-tape-accent outline-none w-32"
        />
      </div>

      {open && shown.length > 0 && (
        <div className="absolute top-full left-0 mt-1 z-30 min-w-[13rem] max-h-64 overflow-y-auto rounded border border-tape-border bg-tape-panel-2 shadow-lg">
          {shown.map((s) => (
            <button
              key={s.value}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => commit(s.value)}
              className="flex w-full items-center justify-between gap-3 px-2.5 py-1.5 text-left hover:bg-tape-panel text-tape-sub"
            >
              <span className="truncate">
                {s.value}
                {s.hint && <span className="text-tape-dim ml-2 text-[10px]">{s.hint}</span>}
              </span>
              <span className="text-tape-faint text-[10px] tabular-nums">{s.count}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
