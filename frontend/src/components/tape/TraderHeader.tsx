import type { TraderAccount } from "@/lib/trader";
import { fmtPct, fmtSignedUsd, fmtUsd } from "@/lib/trader";

/**
 * TRADER portfolio header: equity, cash, buying power, day P&L, and a market
 * open/closed chip. Read-only. When keys aren't configured on the backend the
 * parent renders the "connect Alpaca keys" state instead of this.
 */

function Stat({
  label,
  value,
  tone,
  sub,
}: {
  label: string;
  value: string;
  tone?: "bull" | "bear" | "neutral";
  sub?: string;
}) {
  const color =
    tone === "bull" ? "text-tape-bull" : tone === "bear" ? "text-tape-bear" : "text-tape-text";
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-tape-muted text-[9.5px] tracking-[0.14em] uppercase">{label}</span>
      <span className={`${color} text-[15px] font-semibold tabular-nums`}>{value}</span>
      {sub && <span className="text-tape-faint text-[10px] tabular-nums">{sub}</span>}
    </div>
  );
}

function ClockChip({ isOpen, nextEvent }: { isOpen: boolean; nextEvent: string | null }) {
  return (
    <div className="flex flex-col gap-0.5 items-end">
      <span
        className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded tape-mono text-[10px] font-semibold tracking-[0.1em] border ${
          isOpen
            ? "text-tape-bull border-[#1C3A2C] bg-[rgba(52,211,153,0.12)]"
            : "text-tape-muted border-tape-border bg-tape-panel"
        }`}
      >
        <span
          className={`w-[6px] h-[6px] rounded-full ${isOpen ? "bg-tape-bull tape-pulse" : "bg-tape-muted"}`}
          aria-hidden
        />
        MARKET {isOpen ? "OPEN" : "CLOSED"}
      </span>
      {nextEvent && <span className="text-tape-faint text-[10px]">{nextEvent}</span>}
    </div>
  );
}

export default function TraderHeader({ account }: { account: TraderAccount }) {
  const dayTone = account.day_pl == null ? "neutral" : account.day_pl >= 0 ? "bull" : "bear";
  const nextEvent = account.clock.is_open
    ? account.clock.next_close
      ? `closes ${fmtClock(account.clock.next_close)}`
      : null
    : account.clock.next_open
    ? `opens ${fmtClock(account.clock.next_open)}`
    : null;

  return (
    <div className="shrink-0 flex flex-wrap items-center gap-x-8 gap-y-3 px-[22px] py-3 border-b border-tape-border bg-tape-panel-2">
      <Stat label="Equity" value={fmtUsd(account.equity)} />
      <Stat
        label="Day P&L"
        value={fmtSignedUsd(account.day_pl)}
        tone={dayTone}
        sub={account.day_pl_pct == null ? undefined : fmtPct(account.day_pl_pct)}
      />
      <Stat label="Cash" value={fmtUsd(account.cash)} />
      <Stat label="Buying Power" value={fmtUsd(account.buying_power)} />
      <Stat label="Positions Value" value={fmtUsd(account.long_market_value)} />
      <div className="ml-auto flex items-center gap-6">
        {account.trading_blocked && (
          <span
            className="tape-mono text-[10px] text-tape-warn border border-tape-border rounded px-2 py-0.5"
            title="Alpaca reports trading_blocked on this account"
          >
            TRADING BLOCKED
          </span>
        )}
        <span
          className="tape-mono text-[10px] text-tape-faint"
          title="Read-only view of the Alpaca paper account"
        >
          PAPER {account.account_mask ?? ""} · {account.status ?? "—"}
        </span>
        <ClockChip isOpen={account.clock.is_open} nextEvent={nextEvent} />
      </div>
    </div>
  );
}

/** Local clock time (HH:MM) from an Alpaca ISO timestamp. */
function fmtClock(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "America/New_York",
    });
  } catch {
    return iso.slice(11, 16);
  }
}
