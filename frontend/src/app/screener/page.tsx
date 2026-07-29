"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { loadScreenerData } from "@/lib/tape/screener";
import { fetchUniverseFacets } from "@/lib/universe";
import { watchLive, type QuoteMap } from "@/lib/live";
import { type StatsMap } from "@/lib/tape/screenerStats";
import { NEWS_EVENTS, PIPE_EVENTS, subscribeEvents, type TapeEvent } from "@/lib/events";
import { Density, ScreenerRow } from "@/lib/tape/types";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import HealthStrip from "@/components/tape/HealthStrip";
import ScreenerFilterBar, {
  type ScreenerFilters,
  EMPTY_FILTERS,
} from "@/components/tape/ScreenerFilterBar";
import ScreenerTable from "@/components/tape/ScreenerTable";
import TapeFilterCombo, { type Suggestion } from "@/components/tape/TapeFilterCombo";

/**
 * TAPE_ Screener (design 2a). Rows are the news-driven ticker set with live price
 * + signal + buzz overlays. The filter builder ("+ add filter") composes numeric
 * gates over the screener's own fields; market cap / sector filters need the
 * fundamentals overlay (flagged, blocked on a bulk endpoint).
 */

// The filter ENGINE: a structured filter state -> one AND-combining predicate.
// (Field extraction is the same math as before; only the UI that drives it —
// an always-visible bar instead of a chip builder — changed.) Fundamentals cap
// is $millions -> $B; avg volume is thousands -> M shares; today's volume is
// avg × the relative-volume ratio (both overlays). A set gate excludes rows with
// no data for that field (matches the prior "no data -> filtered out" behavior).
function passes(r: ScreenerRow, f: ScreenerFilters): boolean {
  const ge = (v: number | null, min: number | null) => min == null || (v != null && v >= min);
  const le = (v: number | null, max: number | null) => max == null || (v != null && v <= max);
  const mcapB = r.fundamentals?.marketCap == null ? null : r.fundamentals.marketCap / 1000;
  const avgVolM = r.fundamentals?.avgVolume == null ? null : r.fundamentals.avgVolume / 1000;
  const todayVolM =
    avgVolM != null && r.price.volOverAvg != null ? avgVolM * r.price.volOverAvg : null;
  const pct = r.price.pctChange == null ? null : r.price.pctChange * 100;
  return (
    ge(r.attention.mentions, f.mentionsMin) &&
    ge(r.price.last, f.priceMin) &&
    le(r.price.last, f.priceMax) &&
    ge(pct, f.pctMin) &&
    le(pct, f.pctMax) &&
    ge(todayVolM, f.volMin) &&
    ge(avgVolM, f.avgVolMin) &&
    ge(mcapB, f.mcapMin) &&
    le(mcapB, f.mcapMax) &&
    (!f.sector || r.fundamentals?.sector === f.sector) &&
    (!f.industry || r.fundamentals?.industry === f.industry) &&
    ge(r.attention.sentiment, f.sentMin) &&
    ge(r.attention.heat, f.heatMin) &&
    ge(r.attention.buzzZ, f.buzzMin) &&
    (!f.hasSignal || r.signal.direction !== "none")
  );
}

const INITIAL_FILTERS: ScreenerFilters = { ...EMPTY_FILTERS, mentionsMin: 2 };

type SortKey = "heat" | "mentions" | "sentiment" | "buzzZ";
const SORTS: [SortKey, string][] = [
  ["heat", "Heat"],
  ["mentions", "Density"],
  ["sentiment", "Sentiment"],
  ["buzzZ", "Buzz-z"],
];

function sortVal(r: ScreenerRow, k: SortKey): number {
  if (k === "mentions") return r.attention.mentions;
  if (k === "sentiment") return r.attention.sentiment;
  if (k === "buzzZ") return r.attention.buzzZ ?? -Infinity; // no-baseline sinks to the bottom
  return r.attention.heat;
}

// Coverage window options (hours). Cheap to widen — the endpoint is windowed.
const WINDOWS: [number, string][] = [
  [24, "24h"],
  [48, "48h"],
];

export default function ScreenerPage() {
  const [rows, setRows] = useState<ScreenerRow[]>([]);
  const [stats, setStats] = useState<StatsMap>({});
  const [universe, setUniverse] = useState(0);
  const [live, setLive] = useState(false);
  const [reachable, setReachable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [windowHours, setWindowHours] = useState(48);
  const [filters, setFilters] = useState<ScreenerFilters>(INITIAL_FILTERS);
  const [density, setDensity] = useState<Density>("compact");
  const [sortKey, setSortKey] = useState<SortKey>("heat");
  const [order, setOrder] = useState<"desc" | "asc">("desc");
  const [sectors, setSectors] = useState<string[]>([]);
  const [industries, setIndustries] = useState<string[]>([]);
  // Ticker search chips (LIVE-tape combo style): OR within chips, AND with filters.
  const [searchChips, setSearchChips] = useState<string[]>([]);

  const clock = useClock();

  // A single reload path shared by the interval poll and the SSE accelerator. The
  // latest values are read via refs so the callback identity stays stable (SSE
  // subscriptions don't churn) while always fetching the current window.
  const windowRef = useRef(windowHours);
  windowRef.current = windowHours;
  const inflight = useRef(false);
  const reload = useCallback(async () => {
    if (inflight.current) return; // coalesce overlapping refreshes
    inflight.current = true;
    try {
      const data = await loadScreenerData(windowRef.current);
      setRows(data.rows);
      setStats(data.stats);
      setUniverse(data.universe);
      setLive(data.live);
      setReachable(data.reachable);
    } finally {
      inflight.current = false;
      setLoading(false);
    }
  }, []);

  // Poll (fallback cadence) + immediate reload on window change.
  useEffect(() => {
    setLoading(true);
    reload();
    const t = setInterval(reload, 20_000);
    return () => clearInterval(t);
  }, [reload, windowHours]);

  // SSE accelerator: a new attributed item (news) or a fired catalyst merges into
  // the screener near-real-time. Coalesced with a leading-edge suppress + trailing
  // fire so a busy firehose triggers at most ~one full reload per window (the whole
  // row set is heavy); the 20s poll remains the steady cadence if the stream drops.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const bump = () => {
      if (timer) return;
      timer = setTimeout(() => {
        timer = null;
        reload();
      }, 5000);
    };
    const offNews = subscribeEvents(NEWS_EVENTS, bump, ["news"]);
    const offPipe = subscribeEvents(PIPE_EVENTS, bump, ["fired"]);
    return () => {
      if (timer) clearTimeout(timer);
      offNews();
      offPipe();
    };
  }, [reload]);

  // Sector/industry options for the categorical filters (from the universe facets).
  useEffect(() => {
    fetchUniverseFacets().then((f) => {
      if (!f) return;
      setSectors(f.sectors.map((s) => s.name));
      setIndustries(f.industries);
    });
  }, []);

  // Suggestions for the search combo: rows are already mentions-desc; typing also
  // matches the company name via the fundamentals join (hint).
  const searchSuggestions = useMemo<Suggestion[]>(
    () =>
      rows.map((r) => ({
        value: r.ticker,
        count: r.attention.mentions,
        hint: r.fundamentals?.name ?? undefined,
      })),
    [rows],
  );

  const matched = useMemo(() => {
    const filtered = rows.filter((r) => {
      if (searchChips.length && !searchChips.includes(r.ticker)) return false;
      return passes(r, filters);
    });
    return [...filtered].sort((a, b) =>
      order === "desc"
        ? sortVal(b, sortKey) - sortVal(a, sortKey)
        : sortVal(a, sortKey) - sortVal(b, sortKey),
    );
  }, [rows, filters, searchChips, sortKey, order]);

  // --- real-time quotes: watch the visible set, merge SSE pushes over polls ---
  const [liveQuotes, setLiveQuotes] = useState<QuoteMap>({});
  const [rtLive, setRtLive] = useState(false);

  useEffect(
    () =>
      subscribeEvents(
        PIPE_EVENTS,
        (e: TapeEvent) => {
          const q = e.quotes as QuoteMap | undefined;
          if (q) setLiveQuotes((m) => ({ ...m, ...q }));
        },
        ["quotes"],
      ),
    [],
  );

  // Stable keys: `matched`/`rows` get fresh array identity every 20s poll even
  // when contents are unchanged, which would tear down + re-create these effects
  // (and their intervals) each tick. Depend on the joined ticker set instead.
  const watchKey = useMemo(
    () => matched.slice(0, 150).map((r) => r.ticker).join(","),
    [matched],
  );

  useEffect(() => {
    if (!watchKey) return;
    const tickers = watchKey.split(",");
    let cancelled = false;
    const register = () =>
      watchLive(tickers).then((live) => {
        if (!cancelled) setRtLive(live);
      });
    register();
    const t = setInterval(register, 60_000); // server TTL is 90s
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [watchKey]);

  const rowsLive = useMemo(
    () =>
      matched.map((r) => {
        const q = liveQuotes[r.ticker];
        return q ? { ...r, price: { ...r.price, last: q.price } } : r;
      }),
    [matched, liveQuotes],
  );

  return (
    <>
      <TapeNav active="SCREENER" clock={clock} />
      <ScreenerFilterBar
        filters={filters}
        set={(patch) => setFilters((f) => ({ ...f, ...patch }))}
        onClear={() => setFilters(EMPTY_FILTERS)}
        sectors={sectors}
        industries={industries}
        matched={matched.length}
        total={universe}
        density={density}
        live={live}
        onToggleDensity={() => setDensity((d) => (d === "compact" ? "comfortable" : "compact"))}
        searchSlot={
          <TapeFilterCombo
            label="SEARCH"
            placeholder="ticker / name…"
            suggestions={searchSuggestions}
            selected={searchChips}
            onAdd={(v) => setSearchChips((c) => (c.includes(v) ? c : [...c, v]))}
            onRemove={(v) => setSearchChips((c) => c.filter((x) => x !== v))}
            uppercase
          />
        }
      />

      {/* sort control: rank by density / sentiment / heat (sentiment x density) / buzz-z */}
      <div className="shrink-0 flex items-center gap-2 px-[22px] py-2 border-b border-tape-border-soft bg-tape-panel-2 tape-mono text-[10.5px]">
        <span className="text-tape-muted tracking-[0.1em] font-semibold">RANK BY</span>
        {SORTS.map(([k, label]) => (
          <button
            key={k}
            onClick={() => setSortKey(k)}
            className={`px-2 py-0.5 rounded tracking-[0.06em] ${
              sortKey === k
                ? "bg-tape-panel text-tape-accent border border-tape-border"
                : "text-tape-faint hover:text-tape-sub border border-transparent"
            }`}
          >
            {label}
          </button>
        ))}
        <button
          onClick={() => setOrder((o) => (o === "desc" ? "asc" : "desc"))}
          className="px-1.5 py-0.5 rounded border border-tape-border text-tape-sub hover:border-tape-accent"
          title="toggle sort order"
        >
          {order === "desc" ? "↓" : "↑"}
        </button>

        {/* coverage-window selector: how far back "recent attributed coverage" reaches */}
        <span className="text-tape-muted tracking-[0.1em] font-semibold ml-3">WINDOW</span>
        {WINDOWS.map(([h, label]) => (
          <button
            key={h}
            onClick={() => setWindowHours(h)}
            className={`px-2 py-0.5 rounded tracking-[0.06em] ${
              windowHours === h
                ? "bg-tape-panel text-tape-accent border border-tape-border"
                : "text-tape-faint hover:text-tape-sub border border-transparent"
            }`}
          >
            {label}
          </button>
        ))}

        <span className="text-tape-dim ml-2">
          heat = signed sentiment × density (loud + directional)
        </span>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted">
          loading screener…
        </div>
      ) : !reachable ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-bear px-8 text-center">
          Screener API unreachable — no rows to show. (Prediction API :8001 offline?)
        </div>
      ) : rows.length === 0 ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          No universe tickers have attributed coverage in the last {windowHours}h yet.
        </div>
      ) : matched.length === 0 ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          No tickers match the active filters. Remove a chip above to widen the screen.
        </div>
      ) : (
        <ScreenerTable rows={rowsLive} density={density} stats={stats} />
      )}

      {/* rows meta line */}
      <div className="shrink-0 flex justify-between items-center px-[22px] py-3 tape-mono text-[10.5px] font-medium text-tape-faint border-t border-tape-border-soft">
        <span>
          rows 1–{matched.length} of {rows.length} · ticker column pinned
          {rtLive && Object.keys(liveQuotes).length > 0 && (
            <span className="text-tape-bull">
              {" "}
              · ● {Object.keys(liveQuotes).length} rt quotes (alpaca-iex)
            </span>
          )}
        </span>
        <span>fluid container 1120–1600px</span>
      </div>

      <HealthStrip
        note={
          reachable
            ? `live · /screener/rows · ${windowHours}h window · ${rows.length} universe names w/ coverage`
            : "prediction API offline · no rows"
        }
      />
    </>
  );
}
