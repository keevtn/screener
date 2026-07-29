"use client";

import { useEffect, useState } from "react";
import {
  ExtendedHistory,
  fetchTickerExtended,
  pctStr,
  streakPhrase,
} from "@/lib/extended";

/**
 * Per-ticker EXTENDED-session history for the detail page/modal: the recent days'
 * premarket / regular / afterhours moves, newest first, with the current premarket
 * streak. Honest "--" where a day had no extended prints. Renders nothing until the
 * ticker has at least one logged row (so it stays quiet for untracked names).
 */
export default function ExtendedHistoryStrip({ ticker }: { ticker: string }) {
  const [data, setData] = useState<ExtendedHistory | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    fetchTickerExtended(ticker, 20).then((d) => !cancelled && setData(d));
    return () => {
      cancelled = true;
    };
  }, [ticker]);

  if (!data || !data.reachable || data.count === 0) return null;

  const phrase = streakPhrase(data.premarket_streak);
  const col = (v: number | null | undefined): string =>
    v == null ? "text-tape-faint" : v >= 0 ? "text-tape-bull" : "text-tape-bear";

  return (
    <section className="border-t border-tape-border-soft px-4 py-3">
      <div className="tape-mono text-[9.5px] tracking-[0.14em] text-tape-accent mb-2 flex items-baseline gap-2">
        PREMARKET / EXTENDED HISTORY
        <span className="text-tape-faint tracking-[0.04em]">
          {data.count}d · pre-mkt vs prior close, after-hrs vs close
        </span>
        {phrase && (
          <span
            className={
              data.premarket_streak.direction === "gain" ? "text-tape-bull" : "text-tape-bear"
            }
          >
            · {phrase}
          </span>
        )}
      </div>
      {/* scrolls vertically as history grows (fetches up to 20 sessions) */}
      <div className="overflow-x-auto overflow-y-auto max-h-52">
        <table className="tape-mono text-[10.5px] min-w-[380px]">
          <thead className="sticky top-0 bg-tape-panel">
            <tr className="text-tape-faint tracking-[0.08em] text-[9.5px]">
              <th className="text-left pr-4 pb-1 font-semibold">DATE</th>
              <th className="text-right px-3 pb-1 font-semibold">PRE-MKT</th>
              <th className="text-right px-3 pb-1 font-semibold">REGULAR</th>
              <th className="text-right px-3 pb-1 font-semibold">AFTER-HR</th>
              <th className="text-right pl-3 pb-1 font-semibold">CLOSE</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={r.date} className="border-t border-tape-line">
                <td className="text-left pr-4 py-0.5 text-tape-muted">{r.date.slice(5)}</td>
                <td className={`text-right px-3 py-0.5 font-semibold ${col(r.pm_pct)}`}>
                  {pctStr(r.pm_pct)}
                </td>
                <td className={`text-right px-3 py-0.5 ${col(r.reg_pct)}`}>{pctStr(r.reg_pct)}</td>
                <td className={`text-right px-3 py-0.5 ${col(r.ah_pct)}`}>{pctStr(r.ah_pct)}</td>
                <td className="text-right pl-3 py-0.5 text-tape-sub">
                  {r.reg_close == null ? "--" : r.reg_close.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
