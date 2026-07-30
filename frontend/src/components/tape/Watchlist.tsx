"use client";

import { useCallback, useEffect, useState } from "react";
import { TickerButton } from "@/components/tape/TickerModalProvider";
import { fetchExtendedMovers, type ExtendedRow } from "@/lib/extended";
import {
  fetchWatchlist,
  fmtPct,
  isHttp,
  pinTicker,
  unpinTicker,
  type WatchPin,
} from "@/lib/trader";

/**
 * TRADER watchlist lane (Phase 3), primarily a premarket surface. Pinned tickers
 * (stored in OUR DB) carry an armed/scheduled/watching catalyst read, buzz z, the
 * latest premarket move, and the most-recent catalyst headline. Below, the
 * existing premarket-movers set with a one-click pin. View/stage only — nothing
 * here places an order.
 */

function StateChip({ pin }: { pin: WatchPin }) {
  const map: Record<WatchPin["state"], string> = {
    armed: "text-tape-warn border-tape-border bg-[rgba(240,180,74,0.12)]",
    scheduled: "text-tape-accent border-[#1F3B38] bg-[rgba(79,209,197,0.10)]",
    watching: "text-tape-muted border-tape-border-soft",
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-1.5 py-0.5 rounded tape-mono text-[9.5px] font-semibold uppercase tracking-[0.06em] border ${map[pin.state]}`}
      title={pin.state_label}
    >
      {pin.state === "armed" && <span className="w-[5px] h-[5px] rounded-full bg-tape-warn tape-pulse" aria-hidden />}
      {pin.state_label}
    </span>
  );
}

function pctColor(n: number | null | undefined): string {
  if (n == null) return "text-tape-faint";
  return n >= 0 ? "text-tape-bull" : "text-tape-bear";
}

export default function Watchlist() {
  const [pins, setPins] = useState<WatchPin[]>([]);
  const [movers, setMovers] = useState<ExtendedRow[]>([]);
  const [reachable, setReachable] = useState(true);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const pinned = new Set(pins.map((p) => p.ticker));

  const load = useCallback(async () => {
    const [wl, mv] = await Promise.all([fetchWatchlist(), fetchExtendedMovers(undefined, 30)]);
    setPins(wl.items);
    setReachable(wl.reachable);
    setMovers(mv.movers ?? []);
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  async function addPin(ticker: string) {
    const tk = ticker.trim().toUpperCase();
    if (!tk || busy) return;
    setBusy(true);
    await pinTicker(tk);
    setInput("");
    await load();
    setBusy(false);
  }

  async function removePin(ticker: string) {
    if (busy) return;
    setBusy(true);
    await unpinTicker(ticker);
    await load();
    setBusy(false);
  }

  return (
    <div className="flex flex-col">
      {/* pin input */}
      <div className="flex items-center gap-2 px-[22px] py-3 border-b border-tape-border bg-tape-rail">
        <label htmlFor="pin-input" className="sr-only">
          Pin a ticker to the watchlist
        </label>
        <input
          id="pin-input"
          value={input}
          onChange={(e) => setInput(e.target.value.toUpperCase())}
          onKeyDown={(e) => {
            if (e.key === "Enter") addPin(input);
          }}
          placeholder="PIN TICKER…"
          maxLength={12}
          className="tape-mono text-[11px] bg-tape-panel text-tape-text border border-tape-border rounded px-2 py-1 w-40 focus:outline-none focus:border-tape-accent placeholder:text-tape-dim"
        />
        <button
          onClick={() => addPin(input)}
          disabled={busy || !input.trim()}
          className="tape-mono text-[11px] px-3 py-1 rounded border border-tape-border text-tape-accent hover:bg-tape-panel disabled:opacity-40"
        >
          + pin
        </button>
        <span className="tape-mono text-[10px] text-tape-faint ml-auto">
          {pins.length} pinned · stored in the terminal DB · view/stage only
        </span>
      </div>

      {/* pinned set */}
      {!reachable ? (
        <div className="px-[22px] py-6 tape-mono text-[11px] text-tape-muted text-center">
          Prediction API not reachable on :8001.
        </div>
      ) : pins.length === 0 ? (
        <div className="px-[22px] py-6 tape-mono text-[11px] text-tape-muted text-center">
          No pinned tickers yet — pin names above or from the premarket movers below to arm the lane.
        </div>
      ) : (
        <table className="w-full border-collapse tape-mono text-[11px]">
          <caption className="sr-only">Pinned watchlist tickers with catalyst state</caption>
          <thead>
            <tr className="sticky top-0 bg-tape-panel-2 text-tape-muted text-left tracking-[0.1em] border-b border-tape-border z-10">
              <th scope="col" className="px-4 py-2 font-semibold w-20">TICKER</th>
              <th scope="col" className="px-3 py-2 font-semibold w-52">STATE</th>
              <th scope="col" className="px-2 py-2 font-semibold w-24 text-right">PRE-MKT</th>
              <th scope="col" className="px-2 py-2 font-semibold w-20 text-right">BUZZ z</th>
              <th scope="col" className="px-3 py-2 font-semibold">LATEST CATALYST</th>
              <th scope="col" className="px-2 py-2 font-semibold w-12" aria-label="Unpin" />
            </tr>
          </thead>
          <tbody>
            {pins.map((p) => (
              <tr key={p.ticker} className="border-b border-tape-border-soft hover:bg-tape-panel-2 align-top">
                <td className="px-4 py-2">
                  <TickerButton ticker={p.ticker} className="text-tape-text font-semibold hover:text-tape-accent" />
                </td>
                <td className="px-3 py-2">
                  <StateChip pin={p} />
                </td>
                <td className={`px-2 py-2 text-right tabular-nums ${pctColor(p.premarket?.pm_pct)}`}>
                  {p.premarket?.pm_pct == null ? "—" : fmtPct(p.premarket.pm_pct)}
                </td>
                <td className={`px-2 py-2 text-right tabular-nums ${p.buzz_z == null ? "text-tape-faint" : "text-tape-sub"}`}>
                  {Number.isFinite(p.buzz_z as number) ? (p.buzz_z as number).toFixed(1) : "—"}
                </td>
                <td className="px-3 py-2 max-w-[26rem]">
                  {p.catalyst?.headline ? (
                    <div className="flex flex-col">
                      {isHttp(p.catalyst.url) ? (
                        <a
                          href={p.catalyst.url}
                          target="_blank"
                          rel="noopener noreferrer nofollow"
                          className="text-tape-sub hover:text-tape-accent truncate underline decoration-tape-border-soft underline-offset-2"
                          title={p.catalyst.headline}
                        >
                          {p.catalyst.headline}
                        </a>
                      ) : (
                        <span className="text-tape-sub truncate" title={p.catalyst.headline}>
                          {p.catalyst.headline}
                        </span>
                      )}
                      {p.catalyst.catalyst_type && (
                        <span className="text-tape-dim text-[10px] uppercase tracking-[0.06em]">
                          {p.catalyst.catalyst_type}
                        </span>
                      )}
                    </div>
                  ) : (
                    <span className="text-tape-faint">—</span>
                  )}
                </td>
                <td className="px-2 py-2 text-right">
                  <button
                    onClick={() => removePin(p.ticker)}
                    aria-label={`Unpin ${p.ticker}`}
                    className="tape-mono text-[12px] text-tape-faint hover:text-tape-bear px-1"
                  >
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* premarket movers (existing tracked-set data) with quick-pin */}
      <div className="flex items-center gap-4 px-[22px] py-2 bg-tape-rail border-t border-tape-border mt-1">
        <h3 className="tape-mono text-[11px] font-semibold tracking-[0.14em] text-tape-muted uppercase">
          Premarket Movers
        </h3>
        <span className="tape-mono text-[10px] text-tape-faint">tracked-set premarket moves · pin to watch</span>
      </div>
      {movers.length === 0 ? (
        <div className="px-[22px] py-6 tape-mono text-[11px] text-tape-muted text-center">
          No premarket movers for the latest session.
        </div>
      ) : (
        <table className="w-full border-collapse tape-mono text-[11px]">
          <caption className="sr-only">Premarket movers with pin action</caption>
          <thead>
            <tr className="sticky top-0 bg-tape-panel-2 text-tape-muted text-left tracking-[0.1em] border-b border-tape-border z-10">
              <th scope="col" className="px-4 py-2 font-semibold w-20">TICKER</th>
              <th scope="col" className="px-2 py-2 font-semibold w-24 text-right">PRE-MKT</th>
              <th scope="col" className="px-2 py-2 font-semibold w-24 text-right">REGULAR</th>
              <th scope="col" className="px-2 py-2 font-semibold w-28 text-right">PRIOR CLOSE</th>
              <th scope="col" className="px-2 py-2 font-semibold w-16" aria-label="Pin" />
            </tr>
          </thead>
          <tbody>
            {movers.map((m) => (
              <tr key={m.ticker} className="border-b border-tape-border-soft hover:bg-tape-panel-2">
                <td className="px-4 py-2">
                  <TickerButton ticker={m.ticker} className="text-tape-text font-semibold hover:text-tape-accent" />
                </td>
                <td className={`px-2 py-2 text-right tabular-nums ${pctColor(m.pm_pct)}`}>
                  {m.pm_pct == null ? "—" : fmtPct(m.pm_pct)}
                </td>
                <td className={`px-2 py-2 text-right tabular-nums ${pctColor(m.reg_pct)}`}>
                  {m.reg_pct == null ? "—" : fmtPct(m.reg_pct)}
                </td>
                <td className="px-2 py-2 text-right tabular-nums text-tape-sub">
                  {Number.isFinite(m.prior_close as number) ? (m.prior_close as number).toFixed(2) : "—"}
                </td>
                <td className="px-2 py-2 text-right">
                  {pinned.has(m.ticker) ? (
                    <span className="tape-mono text-[10px] text-tape-accent" title="already pinned">
                      ✓ pinned
                    </span>
                  ) : (
                    <button
                      onClick={() => addPin(m.ticker)}
                      disabled={busy}
                      aria-label={`Pin ${m.ticker}`}
                      className="tape-mono text-[10px] text-tape-faint hover:text-tape-accent disabled:opacity-40"
                    >
                      + pin
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
