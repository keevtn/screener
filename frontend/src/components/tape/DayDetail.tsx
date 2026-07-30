import TraderBlotter from "@/components/tape/TraderBlotter";
import type { DayResult } from "@/lib/trader";
import { fmtPct, fmtSignedUsd } from "@/lib/trader";

/**
 * Detail for one selected calendar day: this account's round-trips exited that
 * day, plus any EOD report cards from sim_daily_summary. Report cards dated
 * before this paper account existed are labeled PRIOR ACCOUNT honestly — the
 * current book didn't produce them.
 */
export default function DayDetail({ day }: { day: DayResult }) {
  const cards = day.report_cards ?? [];
  const trips = day.round_trips ?? [];
  const dayRealized = trips.reduce((s, t) => s + (t.realized_pl ?? 0), 0);

  return (
    <div className="border-t border-tape-border">
      <div className="flex items-center gap-4 px-[22px] py-2 bg-tape-rail">
        <h3 className="tape-mono text-[11px] font-semibold tracking-[0.14em] text-tape-muted uppercase">
          {day.date}
        </h3>
        {trips.length > 0 && (
          <span className="tape-mono text-[10px] text-tape-faint">
            realized{" "}
            <span className={dayRealized >= 0 ? "text-tape-bull" : "text-tape-bear"}>
              {fmtSignedUsd(dayRealized)}
            </span>{" "}
            · {trips.length} round-trips
          </span>
        )}
      </div>

      {/* EOD report cards (our sim_daily_summary rollup) */}
      {cards.length > 0 && (
        <div className="px-[22px] py-3 flex flex-wrap gap-2">
          {cards.map((c) => (
            <div
              key={`${c.session_date}-${c.config_id}`}
              className="border border-tape-border-soft rounded p-2.5 min-w-[190px] bg-tape-panel"
            >
              <div className="flex items-center gap-2 mb-1.5">
                <span className="tape-mono text-[11px] text-tape-text font-semibold">{c.config_name}</span>
                {c.prior_account && (
                  <span
                    className="tape-mono text-[8.5px] uppercase tracking-[0.06em] text-tape-warn border border-tape-border rounded px-1 py-px"
                    title="This report card predates the current paper account — it's from the prior portfolio."
                  >
                    prior account
                  </span>
                )}
              </div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 tape-mono text-[10px] text-tape-faint">
                <span>
                  P&L{" "}
                  <span className={(c.pnl_dollars ?? 0) >= 0 ? "text-tape-bull" : "text-tape-bear"}>
                    {fmtSignedUsd(c.pnl_dollars)}
                  </span>
                </span>
                <span>
                  trades <span className="text-tape-sub">{c.trades}</span>
                </span>
                <span>
                  hit <span className="text-tape-sub">{c.hit_rate == null ? "—" : fmtPct(c.hit_rate, 0)}</span>
                </span>
                <span>
                  W/L <span className="text-tape-sub">{c.wins}/{c.losses}</span>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* This account's round-trips for the day */}
      {trips.length > 0 ? (
        <TraderBlotter items={trips} />
      ) : (
        <div className="px-[22px] py-6 tape-mono text-[11px] text-tape-muted text-center">
          {cards.length > 0
            ? "No round-trips on this paper account for this day (report card above is from the prior portfolio)."
            : "No trades on this day."}
        </div>
      )}
    </div>
  );
}
