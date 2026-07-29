"use client";

import { ReactNode, createContext, useCallback, useContext, useEffect, useState } from "react";
import Link from "next/link";
import TickerDetailBody from "@/components/tape/TickerDetailBody";

/**
 * App-wide ticker pop-up. Any component calls useTicker().open(symbol) to open a
 * single shared modal showing the full ticker detail (the same TickerDetailBody
 * as /ticker/[t]) — so a ticker looks and behaves identically wherever it appears
 * (LIVE tape, catalysts, screener, universe, ledger). The full page stays at
 * /ticker/[t] and is one click away via "full page ↗".
 */

interface TickerCtx {
  open: (ticker: string) => void;
}

const Ctx = createContext<TickerCtx>({ open: () => {} });

export function useTicker(): TickerCtx {
  return useContext(Ctx);
}

export function TickerModalProvider({ children }: { children: ReactNode }) {
  const [symbol, setSymbol] = useState<string | null>(null);
  const open = useCallback((t: string) => setSymbol(t.trim().toUpperCase()), []);
  const close = useCallback(() => setSymbol(null), []);

  useEffect(() => {
    if (!symbol) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [symbol, close]);

  return (
    <Ctx.Provider value={{ open }}>
      {children}
      {symbol && (
        <div
          className="tape fixed inset-0 z-50 flex items-start justify-center p-4 sm:p-8"
          role="dialog"
          aria-modal="true"
          aria-label={`${symbol} detail`}
        >
          <div className="absolute inset-0 bg-black/60" onClick={close} />
          <div className="relative z-10 flex flex-col w-full max-w-[980px] max-h-[90vh] rounded-lg border border-tape-border bg-tape-bg shadow-2xl overflow-hidden">
            <div className="shrink-0 flex items-center gap-3 px-4 py-2 border-b border-tape-border-soft bg-tape-panel-2 tape-mono text-[10.5px]">
              <span className="text-tape-muted tracking-[0.14em] font-semibold">TICKER</span>
              <Link
                href={`/ticker/${symbol}`}
                onClick={close}
                className="ml-auto text-tape-faint hover:text-tape-accent"
              >
                full page ↗
              </Link>
              <button
                onClick={close}
                aria-label="close"
                className="text-tape-faint hover:text-tape-bear text-[13px] leading-none px-1"
              >
                ✕
              </button>
            </div>
            <TickerDetailBody ticker={symbol} />
          </div>
        </div>
      )}
    </Ctx.Provider>
  );
}

/**
 * A ticker name that opens the pop-up on click. Drop-in for the many places a
 * ticker renders. `as` lets callers pick the element look; defaults to a button
 * styled like the surrounding text.
 */
export function TickerButton({
  ticker,
  className = "",
  style,
  children,
}: {
  ticker: string;
  className?: string;
  style?: React.CSSProperties;
  children?: ReactNode;
}) {
  const { open } = useTicker();
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        open(ticker);
      }}
      className={className}
      style={style}
      title={`${ticker} detail`}
    >
      {children ?? ticker}
    </button>
  );
}
