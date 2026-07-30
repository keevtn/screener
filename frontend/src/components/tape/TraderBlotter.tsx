import { TickerButton } from "@/components/tape/TickerModalProvider";
import TraderProvenance from "@/components/tape/TraderProvenance";
import type { RoundTrip } from "@/lib/trader";
import { fmtPct, fmtSignedUsd, fmtStamp, fmtUsd } from "@/lib/trader";

/**
 * The trade blotter: filled fills grouped into round-trips (entry -> exit) with
 * realized P&L, each joined back to the sim config + originating catalyst. The
 * Alpaca account is the source of truth; provenance is the DB join. Read-only.
 */
export default function TraderBlotter({ items }: { items: RoundTrip[] }) {
  return (
    <table className="w-full border-collapse tape-mono text-[11px]">
      <caption className="sr-only">Closed paper-trading round-trips with realized profit and loss</caption>
      <thead>
        <tr className="sticky top-0 bg-tape-panel-2 text-tape-muted text-left tracking-[0.1em] border-b border-tape-border z-10">
          <th scope="col" className="px-4 py-2 font-semibold w-20">TICKER</th>
          <th scope="col" className="px-2 py-2 font-semibold w-16">DIR</th>
          <th scope="col" className="px-2 py-2 font-semibold w-14 text-right">QTY</th>
          <th scope="col" className="px-3 py-2 font-semibold w-24 text-right">ENTRY</th>
          <th scope="col" className="px-3 py-2 font-semibold w-24 text-right">EXIT</th>
          <th scope="col" className="px-3 py-2 font-semibold w-32">EXITED</th>
          <th scope="col" className="px-3 py-2 font-semibold w-32 text-right">REALIZED P&L</th>
          <th scope="col" className="px-3 py-2 font-semibold">PROVENANCE</th>
        </tr>
      </thead>
      <tbody>
        {items.map((t, i) => {
          const isLong = t.direction >= 0;
          const plTone = t.realized_pl >= 0 ? "text-tape-bull" : "text-tape-bear";
          return (
            <tr
              key={`${t.ticker}-${t.exit_order_id ?? i}`}
              className="border-b border-tape-border-soft hover:bg-tape-panel-2 align-top"
            >
              <td className="px-4 py-2">
                <TickerButton ticker={t.ticker} className="text-tape-text font-semibold hover:text-tape-accent" />
              </td>
              <td className={`px-2 py-2 font-semibold ${isLong ? "text-tape-bull" : "text-tape-bear"}`}>
                {isLong ? "▲ LONG" : "▼ SHORT"}
              </td>
              <td className="px-2 py-2 text-right text-tape-sub tabular-nums">{t.qty}</td>
              <td className="px-3 py-2 text-right text-tape-sub tabular-nums">{fmtUsd(t.entry_price)}</td>
              <td className="px-3 py-2 text-right text-tape-sub tabular-nums">{fmtUsd(t.exit_price)}</td>
              <td className="px-3 py-2 text-tape-faint tabular-nums">{fmtStamp(t.exit_time)}</td>
              <td className={`px-3 py-2 text-right tabular-nums font-semibold ${plTone}`}>
                <div className="flex flex-col items-end">
                  <span>{fmtSignedUsd(t.realized_pl)}</span>
                  <span className="text-[10px] font-normal">{fmtPct(t.realized_pl_pct)}</span>
                </div>
              </td>
              <td className="px-3 py-2">
                <TraderProvenance prov={t.provenance} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
