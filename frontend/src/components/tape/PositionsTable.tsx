import { TickerButton } from "@/components/tape/TickerModalProvider";
import TraderProvenance from "@/components/tape/TraderProvenance";
import type { Position } from "@/lib/trader";
import { fmtPct, fmtSignedUsd, fmtUsd } from "@/lib/trader";

/**
 * Live open positions with unrealized P&L, each joined to its originating config
 * + catalyst. Read-only. Empty state is handled by the parent so a fresh account
 * reads as intentional.
 */
export default function PositionsTable({ items }: { items: Position[] }) {
  return (
    <table className="w-full border-collapse tape-mono text-[11px]">
      <caption className="sr-only">Open paper-trading positions with unrealized profit and loss</caption>
      <thead>
        <tr className="sticky top-0 bg-tape-panel-2 text-tape-muted text-left tracking-[0.1em] border-b border-tape-border z-10">
          <th scope="col" className="px-4 py-2 font-semibold w-20">TICKER</th>
          <th scope="col" className="px-2 py-2 font-semibold w-16">SIDE</th>
          <th scope="col" className="px-2 py-2 font-semibold w-16 text-right">QTY</th>
          <th scope="col" className="px-3 py-2 font-semibold w-24 text-right">AVG ENTRY</th>
          <th scope="col" className="px-3 py-2 font-semibold w-24 text-right">CURRENT</th>
          <th scope="col" className="px-3 py-2 font-semibold w-28 text-right">MKT VALUE</th>
          <th scope="col" className="px-3 py-2 font-semibold w-32 text-right">UNREAL P&L</th>
          <th scope="col" className="px-3 py-2 font-semibold">PROVENANCE</th>
        </tr>
      </thead>
      <tbody>
        {items.map((p) => {
          const plTone =
            p.unrealized_pl == null ? "text-tape-faint" : p.unrealized_pl >= 0 ? "text-tape-bull" : "text-tape-bear";
          const isShort = (p.side ?? "").toLowerCase() === "short";
          return (
            <tr key={p.ticker} className="border-b border-tape-border-soft hover:bg-tape-panel-2 align-top">
              <td className="px-4 py-2">
                <TickerButton ticker={p.ticker} className="text-tape-text font-semibold hover:text-tape-accent" />
              </td>
              <td className={`px-2 py-2 font-semibold ${isShort ? "text-tape-bear" : "text-tape-bull"}`}>
                {isShort ? "SHORT" : "LONG"}
              </td>
              <td className="px-2 py-2 text-right text-tape-sub tabular-nums">{p.qty ?? "—"}</td>
              <td className="px-3 py-2 text-right text-tape-sub tabular-nums">{fmtUsd(p.avg_entry_price)}</td>
              <td className="px-3 py-2 text-right text-tape-text tabular-nums">{fmtUsd(p.current_price)}</td>
              <td className="px-3 py-2 text-right text-tape-sub tabular-nums">{fmtUsd(p.market_value)}</td>
              <td className={`px-3 py-2 text-right tabular-nums font-semibold ${plTone}`}>
                <div className="flex flex-col items-end">
                  <span>{fmtSignedUsd(p.unrealized_pl)}</span>
                  <span className="text-[10px] font-normal">{fmtPct(p.unrealized_pl_pct)}</span>
                </div>
              </td>
              <td className="px-3 py-2">
                <TraderProvenance prov={p.provenance} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
