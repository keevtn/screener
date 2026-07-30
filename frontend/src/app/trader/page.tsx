"use client";

import { useEffect, useMemo, useState } from "react";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import HealthStrip from "@/components/tape/HealthStrip";
import TraderHeader from "@/components/tape/TraderHeader";
import EquityCurve from "@/components/tape/EquityCurve";
import PositionsTable from "@/components/tape/PositionsTable";
import TraderBlotter from "@/components/tape/TraderBlotter";
import PnlCalendar from "@/components/tape/PnlCalendar";
import DayDetail from "@/components/tape/DayDetail";
import Watchlist from "@/components/tape/Watchlist";
import {
  CURVE_TIMEFRAMES,
  fetchBlotter,
  fetchCalendar,
  fetchDay,
  fetchPortfolioHistory,
  fetchPositions,
  fetchTraderAccount,
  type BlotterResult,
  type BlotterScope,
  type CalendarResult,
  type DayResult,
  type PortfolioHistory,
  type PositionsResult,
  type TraderAccount,
} from "@/lib/trader";

/**
 * TAPE_ TRADER — read-only view of the Alpaca paper account (Phase 1).
 *
 * Portfolio header, equity curve (timeframe dropdown), live positions, and a
 * round-trip blotter. Every row joins back to OUR DB for provenance: the sim
 * config that opened it + the originating catalyst headline. Strictly read-only —
 * order placement stays with the local driver + human gate. Keys live only on the
 * backend; this page talks to /trader/* on the prediction API.
 *
 * A fresh paper account (no keys, no history, no trades) renders honest empty
 * states, not errors — and lights up as the driver trades.
 */

const SCOPES: { key: BlotterScope; label: string }[] = [
  { key: "closed", label: "closed" },
  { key: "today", label: "today" },
  { key: "open", label: "open" },
  { key: "all", label: "all" },
];

const ACCOUNT_POLL_MS = 12_000;
const BLOTTER_POLL_MS = 30_000;
const CURVE_POLL_MS = 60_000;

function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="border-b border-tape-border">
      <div className="flex items-center gap-4 px-[22px] py-2 bg-tape-rail">
        <h2 className="tape-mono text-[11px] font-semibold tracking-[0.14em] text-tape-muted uppercase">{title}</h2>
        {right && <div className="ml-auto flex items-center gap-2">{right}</div>}
      </div>
      {children}
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="px-[22px] py-8 tape-mono text-[11px] text-tape-muted text-center">{children}</div>;
}

export default function TraderPage() {
  const clock = useClock();
  const [account, setAccount] = useState<TraderAccount | null>(null);
  const [positions, setPositions] = useState<PositionsResult | null>(null);
  const [history, setHistory] = useState<PortfolioHistory | null>(null);
  const [blotter, setBlotter] = useState<BlotterResult | null>(null);
  const [loading, setLoading] = useState(true);

  const [tf, setTf] = useState(CURVE_TIMEFRAMES[2]); // default 1M
  const [scope, setScope] = useState<BlotterScope>("closed");
  const [configFilter, setConfigFilter] = useState<string>(""); // config_id, "" = all

  // History view (Phase 2): P&L calendar + selected-day detail.
  const [view, setView] = useState<"overview" | "history" | "watchlist">("overview");
  const [monthDate, setMonthDate] = useState<Date>(() => new Date());
  const [calendar, setCalendar] = useState<CalendarResult | null>(null);
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [dayData, setDayData] = useState<DayResult | null>(null);

  // Account + positions poll together (the live pulse of the page).
  useEffect(() => {
    let cancelled = false;
    async function load() {
      const [a, p] = await Promise.all([fetchTraderAccount(), fetchPositions()]);
      if (cancelled) return;
      setAccount(a);
      setPositions(p);
      setLoading(false);
    }
    load();
    const t = setInterval(load, ACCOUNT_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  // Equity curve refetches on timeframe change + a slow poll.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      const h = await fetchPortfolioHistory(tf.period, tf.timeframe);
      if (!cancelled) setHistory(h);
    }
    load();
    const t = setInterval(load, CURVE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [tf]);

  // Blotter refetches on scope change + a poll (config filter is applied client-side).
  useEffect(() => {
    let cancelled = false;
    async function load() {
      const b = await fetchBlotter(scope);
      if (!cancelled) setBlotter(b);
    }
    load();
    const t = setInterval(load, BLOTTER_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [scope]);

  // Calendar loads for the displayed month while the History view is active.
  useEffect(() => {
    if (view !== "history") return;
    let cancelled = false;
    const first = `${monthDate.getFullYear()}-${String(monthDate.getMonth() + 1).padStart(2, "0")}-01`;
    const lastDay = new Date(monthDate.getFullYear(), monthDate.getMonth() + 1, 0).getDate();
    const last = `${monthDate.getFullYear()}-${String(monthDate.getMonth() + 1).padStart(2, "0")}-${String(lastDay).padStart(2, "0")}`;
    fetchCalendar(first, last).then((c) => {
      if (!cancelled) setCalendar(c);
    });
    return () => {
      cancelled = true;
    };
  }, [view, monthDate]);

  // Selected-day detail.
  useEffect(() => {
    if (!selectedDay) {
      setDayData(null);
      return;
    }
    let cancelled = false;
    fetchDay(selectedDay).then((d) => {
      if (!cancelled) setDayData(d);
    });
    return () => {
      cancelled = true;
    };
  }, [selectedDay]);

  // Config options derived from whatever provenance the blotter surfaced.
  const configOptions = useMemo(() => {
    const m = new Map<string, string>();
    for (const it of blotter?.items ?? []) {
      const pv = it.provenance;
      if (pv?.config_id) m.set(pv.config_id, pv.config_name ?? pv.config_id);
    }
    return Array.from(m, ([id, name]) => ({ id, name }));
  }, [blotter]);

  const blotterItems = useMemo(() => {
    const items = blotter?.items ?? [];
    if (!configFilter) return items;
    return items.filter((it) => it.provenance?.config_id === configFilter);
  }, [blotter, configFilter]);

  const configured = account?.configured ?? false;
  const reachable = account?.reachable ?? true;

  return (
    <>
      <TapeNav active="TRADER" clock={clock} />

      {loading ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted">
          loading trader…
        </div>
      ) : !reachable ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          Prediction API not reachable on :8001 — start it with scripts/serve_api.py.
        </div>
      ) : !configured ? (
        <div className="flex-1 flex flex-col items-center justify-center gap-2 tape-mono text-[11px] text-tape-muted px-8 text-center">
          <span className="text-tape-sub text-[13px] font-semibold tracking-[0.08em]">CONNECT ALPACA KEYS</span>
          <span className="max-w-md">
            The TRADER view is read-only against an Alpaca <span className="text-tape-accent">paper</span> account.
            Set <span className="text-tape-sub">ALPACA_API_KEY</span> and{" "}
            <span className="text-tape-sub">ALPACA_API_SECRET</span> on the API service to light it up.
          </span>
        </div>
      ) : (
        <>
          {account && <TraderHeader account={account} />}

          <div
            className="shrink-0 flex items-center gap-1 px-[22px] py-2 border-b border-tape-border bg-tape-panel-2"
            role="group"
            aria-label="Trader view"
          >
            {(["overview", "history", "watchlist"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                aria-pressed={view === v}
                className={`px-3 py-1 rounded tape-mono text-[11px] tracking-[0.1em] uppercase ${
                  view === v
                    ? "bg-tape-panel text-tape-accent border border-tape-border"
                    : "text-tape-faint hover:text-tape-sub border border-transparent"
                }`}
              >
                {v}
              </button>
            ))}
          </div>

          <div className="flex-1 overflow-y-auto">
            {view === "watchlist" ? (
              <Watchlist />
            ) : view === "history" ? (
              <>
                <PnlCalendar
                  monthDate={monthDate}
                  data={calendar}
                  selected={selectedDay}
                  onSelect={setSelectedDay}
                  onPrev={() => setMonthDate((d) => new Date(d.getFullYear(), d.getMonth() - 1, 1))}
                  onNext={() => setMonthDate((d) => new Date(d.getFullYear(), d.getMonth() + 1, 1))}
                />
                {calendar && calendar.available === false && (
                  <Empty>P&L calendar temporarily unavailable.</Empty>
                )}
                {selectedDay && dayData ? (
                  <DayDetail day={dayData} />
                ) : (
                  <div className="px-[22px] py-6 tape-mono text-[11px] text-tape-faint text-center border-t border-tape-border">
                    Select a day to see its round-trips and EOD report card.
                  </div>
                )}
              </>
            ) : (
            <>
            {/* EQUITY CURVE */}
            <Section
              title="Equity Curve"
              right={
                <>
                  <label htmlFor="tf-select" className="sr-only">
                    Equity curve timeframe
                  </label>
                  <select
                    id="tf-select"
                    value={tf.label}
                    onChange={(e) => {
                      const next = CURVE_TIMEFRAMES.find((x) => x.label === e.target.value);
                      if (next) setTf(next);
                    }}
                    className="tape-mono text-[11px] bg-tape-panel text-tape-sub border border-tape-border rounded px-2 py-1 focus:outline-none focus:border-tape-accent"
                  >
                    {CURVE_TIMEFRAMES.map((x) => (
                      <option key={x.label} value={x.label}>
                        {x.label}
                      </option>
                    ))}
                  </select>
                </>
              }
            >
              {history && history.available === false ? (
                <Empty>Equity history temporarily unavailable.</Empty>
              ) : (history?.points.filter((p) => p.equity != null).length ?? 0) < 2 ? (
                <Empty>No equity history yet — the curve fills in as the account trades.</Empty>
              ) : (
                <div className="px-[10px] py-2">
                  <EquityCurve points={history!.points} />
                </div>
              )}
            </Section>

            {/* POSITIONS */}
            <Section
              title="Positions"
              right={
                <span className="tape-mono text-[10px] text-tape-faint">{positions?.count ?? 0} open</span>
              }
            >
              {positions && positions.available === false ? (
                <Empty>Positions temporarily unavailable.</Empty>
              ) : (positions?.count ?? 0) === 0 ? (
                <Empty>No open positions.</Empty>
              ) : (
                <PositionsTable items={positions!.items} />
              )}
            </Section>

            {/* BLOTTER */}
            <Section
              title="Blotter"
              right={
                <>
                  <div className="flex items-center gap-1" role="group" aria-label="Filter blotter by scope">
                    {SCOPES.map((s) => (
                      <button
                        key={s.key}
                        onClick={() => setScope(s.key)}
                        aria-pressed={scope === s.key}
                        className={`px-2.5 py-1 rounded tape-mono text-[11px] tracking-[0.08em] uppercase ${
                          scope === s.key
                            ? "bg-tape-panel text-tape-accent border border-tape-border"
                            : "text-tape-faint hover:text-tape-sub border border-transparent"
                        }`}
                      >
                        {s.label}
                      </button>
                    ))}
                  </div>
                  {configOptions.length > 0 && (
                    <>
                      <label htmlFor="cfg-select" className="sr-only">
                        Filter by sim config
                      </label>
                      <select
                        id="cfg-select"
                        value={configFilter}
                        onChange={(e) => setConfigFilter(e.target.value)}
                        className="tape-mono text-[11px] bg-tape-panel text-tape-sub border border-tape-border rounded px-2 py-1 focus:outline-none focus:border-tape-accent"
                      >
                        <option value="">all configs</option>
                        {configOptions.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name}
                          </option>
                        ))}
                      </select>
                    </>
                  )}
                </>
              }
            >
              {blotter && blotter.available === false ? (
                <Empty>Blotter temporarily unavailable.</Empty>
              ) : blotterItems.length === 0 ? (
                <Empty>
                  {scope === "open"
                    ? "No open positions."
                    : configFilter
                    ? "No round-trips for this config in the current view."
                    : "No round-trips yet — the blotter fills as the agent trades this account."}
                </Empty>
              ) : scope === "open" ? (
                // 'open' scope returns positions; render them in the positions grid.
                <PositionsTable items={(positions?.items ?? [])} />
              ) : (
                <TraderBlotter items={blotterItems} />
              )}
            </Section>
            </>
            )}
          </div>
        </>
      )}

      <HealthStrip
        note={
          !reachable
            ? "prediction API offline :8001"
            : !configured
            ? "TRADER read-only · Alpaca paper keys not configured on the API"
            : "TRADER read-only · Alpaca paper account · provenance joined from sim ledger"
        }
      />
    </>
  );
}
