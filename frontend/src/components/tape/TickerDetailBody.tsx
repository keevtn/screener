"use client";

import { useEffect, useState } from "react";
import TickerChart from "@/components/tape/TickerChart";
import PriceChart from "@/components/tape/PriceChart";
import IntradayDensity, {
  hourlyBuckets,
  messageTotals,
  type HourBucket,
  type DensityTotals,
} from "@/components/tape/IntradayDensity";
import DeepDive from "@/components/tape/DeepDive";
import LiveUnifiedChart from "@/components/tape/LiveUnifiedChart";
import TickerInsightPanel from "@/components/tape/TickerInsightPanel";
import ExtendedHistoryStrip from "@/components/tape/ExtendedHistoryStrip";
import SearchInterestPanel, { searchBuckets } from "@/components/tape/SearchInterestPanel";
import {
  fetchTickerSeries,
  fetchTickerClusters,
  fetchIntradayLive,
  fetchIntradayBars,
  fetchSearchHourly,
  type TickerSeries,
  type ClusterItem,
  type IntradayBars,
  type SearchHourly,
} from "@/lib/ticker";
import { fetchNewsForTicker } from "@/lib/api";
import { toneColor, toneTag } from "@/lib/tape/insight";
import { fmtET } from "@/lib/tape/time";

/**
 * Ticker detail — price (real $ + date axes, candle/line), attention (news volume
 * + buzz-z + sentiment with labelled axes), live intraday density, associated
 * news clusters, and the AI deep dive. Shared by the /ticker/[t] full page and
 * the pop-up modal, so a ticker looks identical wherever it is opened.
 */

// Chart timeframe dropdown. Price bars in the parquet cache are DAILY, so 1D
// cannot honestly render candles — it switches the panel to the last close +
// live intraday density view (clearly labeled), never fabricated intraday bars.
const TIMEFRAMES = [
  { key: "1D", days: 1 },
  { key: "1W", days: 7 },
  { key: "2W", days: 14 },
  { key: "1M", days: 30 },
  { key: "2M", days: 60 },
  { key: "6M", days: 180 },
  { key: "1Y", days: 365 },
] as const;
type TfKey = (typeof TIMEFRAMES)[number]["key"];

export default function TickerDetailBody({ ticker }: { ticker: string }) {
  const [tf, setTf] = useState<TfKey>("2M");
  const tfDays = TIMEFRAMES.find((t) => t.key === tf)!.days;
  // The series endpoint has a 5-day floor; fetch at least a week and slice
  // client-side so short timeframes stay exact.
  const days = Math.max(tfDays, 7);
  const [data, setData] = useState<TickerSeries | null>(null);
  const [loading, setLoading] = useState(true);
  const [reachable, setReachable] = useState(true);
  const [intradayHours, setIntradayHours] = useState(48);
  const [buckets, setBuckets] = useState<HourBucket[]>([]);
  const [msgTotals, setMsgTotals] = useState<DensityTotals | null>(null);
  const [chartMode, setChartMode] = useState<"candle" | "line">("candle");
  const [clusters, setClusters] = useState<ClusterItem[]>([]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchTickerSeries(ticker, days).then((d) => {
      if (cancelled) return;
      setData(d);
      setReachable(d !== null);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [ticker, days]);

  useEffect(() => {
    let cancelled = false;
    fetchTickerClusters(ticker, 40).then((c) => {
      if (!cancelled) setClusters(c);
    });
    return () => {
      cancelled = true;
    };
  }, [ticker]);

  useEffect(() => {
    let cancelled = false;
    const pull = () =>
      fetchNewsForTicker(ticker, 500).then((items) => {
        if (cancelled) return;
        const now = Date.now();
        setBuckets(hourlyBuckets(items, intradayHours, now));
        setMsgTotals(messageTotals(items, now)); // 24h/48h totals, toggle-independent
      });
    pull();
    const t = setInterval(pull, 20_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [ticker, intradayHours]);

  // On-demand HOURLY Google-Trends search interest for the viewed ticker only
  // (mirrors the intraday-bars pattern: server TTL-cached, fail-soft). NOT polled
  // — search interest moves at most hourly and the unofficial endpoint won't
  // tolerate hammering; refetch only when the ticker or the 24/48h window changes.
  const [searchHourly, setSearchHourly] = useState<SearchHourly | null>(null);
  useEffect(() => {
    let cancelled = false;
    setSearchHourly(null);
    fetchSearchHourly(ticker, intradayHours).then((r) => {
      if (!cancelled) setSearchHourly(r);
    });
    return () => {
      cancelled = true;
    };
  }, [ticker, intradayHours]);
  const sBuckets = searchHourly ? searchBuckets(searchHourly.points) : [];

  // Live current-hour mention counter from the Redis ingest counters
  // (null = Redis unavailable -> the panel just shows the bucketed view).
  const [liveNow, setLiveNow] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    const pull = () =>
      fetchIntradayLive(ticker).then((r) => {
        if (!cancelled) setLiveNow(r.live ? (r.items.at(-1)?.count ?? 0) : null);
      });
    pull();
    const t = setInterval(pull, 20_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [ticker]);

  const lastPrice = data?.price.at(-1);
  const lastAtt = data?.attention.at(-1);
  const latestBuzz = [...(data?.attention ?? [])].reverse().find((a) => a.buzz_z != null)?.buzz_z;

  // Real intraday candles for the short timeframes (yfinance 1m/5m, TTL-cached
  // server-side, fetched per viewed ticker only). null while loading; falls back
  // to last-close (1D) or daily candles (1W) when unavailable — never synthetic.
  const [ibars, setIbars] = useState<IntradayBars | null>(null);
  useEffect(() => {
    if (tf !== "1D" && tf !== "1W") {
      setIbars(null);
      return;
    }
    let cancelled = false;
    setIbars(null);
    fetchIntradayBars(ticker, tf === "1D" ? "1d" : "1w").then((r) => {
      if (!cancelled) setIbars(r);
    });
    return () => {
      cancelled = true;
    };
  }, [ticker, tf]);

  // Slice every panel to the selected timeframe (the fetch has a 7-day floor).
  const cutoffIso = new Date(Date.now() - tfDays * 864e5).toISOString().slice(0, 10);
  const priceView = (data?.price ?? []).filter((p) => p.date >= cutoffIso);
  const attentionView = (data?.attention ?? []).filter((a) => a.date >= cutoffIso);
  const clustersView = clusters.filter(
    (c) => Date.now() - Date.parse(c.published_at) <= tfDays * 864e5,
  );
  const isIntradayTf = tf === "1D";

  return (
    <div className="flex flex-col min-h-0 flex-1">
      {/* header: ticker + price + baseline + range */}
      <div className="shrink-0 flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3 border-b border-tape-border bg-tape-panel-2 tape-mono text-[11px]">
        <span className="text-tape-text text-[15px] font-bold tracking-[0.06em]">{ticker}</span>
        {lastPrice && (
          <span className="text-tape-sub">
            {lastPrice.close.toFixed(2)}{" "}
            <span className="text-tape-dim text-[10px]">@ {lastPrice.date}</span>
          </span>
        )}
        {data?.baseline ? (
          <span className="text-tape-faint">
            buzz baseline <span className="text-tape-sub">{data.baseline.mean.toFixed(1)}</span>
            /day ±{data.baseline.std.toFixed(1)} · n={data.baseline.n_days} [{data.baseline.source}]
          </span>
        ) : (
          <span className="text-tape-dim">no buzz baseline (price-only)</span>
        )}
        {latestBuzz != null && (
          <span className={latestBuzz >= 1 ? "text-tape-warn" : "text-tape-faint"}>
            latest buzz-z {latestBuzz >= 0 ? "+" : ""}
            {latestBuzz.toFixed(1)}
          </span>
        )}
        <span className="ml-auto flex items-center gap-1.5 text-tape-faint">
          timeframe
          <select
            value={tf}
            onChange={(e) => setTf(e.target.value as TfKey)}
            className="bg-tape-panel border border-tape-border rounded px-2 py-1 text-tape-text focus:border-tape-accent outline-none tracking-[0.08em]"
          >
            {TIMEFRAMES.map((t) => (
              <option key={t.key} value={t.key}>
                {t.key}
              </option>
            ))}
          </select>
        </span>
      </div>

      <div className="flex-1 overflow-auto">
        {/* live INSIGHT band — independent fetch, shows while the series loads */}
        <TickerInsightPanel ticker={ticker} clusters={clusters} />
        {/* extended-session history — quiet (renders nothing) for untracked names */}
        <ExtendedHistoryStrip ticker={ticker} />
        {loading ? (
          <div className="flex items-center justify-center tape-mono text-[11px] text-tape-muted py-16">
            loading {ticker}…
          </div>
        ) : !reachable ? (
          <div className="flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 py-16 text-center">
            Prediction API not reachable on :8001 — start it with scripts/serve_api.py.
          </div>
        ) : (
          <div className="p-4">
            {/* PRICE — real axes via lightweight-charts, candle/line.
                1D/1W use REAL intraday bars (yfinance 1m/5m) when available;
                1D falls back to last close, 1W to daily candles. */}
            {(() => {
              const wantIntraday = tf === "1D" || tf === "1W";
              const intradayReady = wantIntraday && !!ibars?.available;
              const intradayLoading = wantIntraday && ibars === null;
              const showToggle = !isIntradayTf || intradayReady;
              const axisNote = intradayReady
                ? `${ibars!.interval} bars · pre/after-hours dimmed · times ET`
                : isIntradayTf
                ? "1D — no intraday bars available for this ticker (never faked)"
                : "drag to pan · scroll to zoom · $ / date axes";
              return (
                <>
                  <div className="flex items-center gap-2 px-2 mb-1 tape-mono text-[10.5px]">
                    <span className="text-tape-muted tracking-[0.1em] font-semibold">
                      PRICE · {intradayReady ? `${ibars!.interval} intraday` : "adj close"}
                    </span>
                    {showToggle &&
                      (["candle", "line"] as const).map((m) => (
                        <button
                          key={m}
                          onClick={() => setChartMode(m)}
                          className={`px-2 py-0.5 rounded uppercase tracking-[0.06em] ${
                            chartMode === m
                              ? "bg-tape-panel text-tape-accent border border-tape-border"
                              : "text-tape-faint hover:text-tape-sub border border-transparent"
                          }`}
                        >
                          {m}
                        </button>
                      ))}
                    <span className="text-tape-dim ml-2">{axisNote}</span>
                  </div>
                  {intradayReady ? (
                    <PriceChart price={priceView} mode={chartMode} intraday={ibars!.bars} />
                  ) : intradayLoading ? (
                    <div className="px-2 py-8 tape-mono text-[10.5px] text-tape-muted text-center">
                      fetching intraday bars…
                    </div>
                  ) : isIntradayTf ? (
                    <div className="px-2 py-5 tape-mono text-[11px] text-tape-sub border border-tape-border-soft rounded bg-tape-panel-2">
                      last close{" "}
                      <span className="text-tape-text text-[14px] font-bold">
                        {lastPrice ? `$${lastPrice.close.toFixed(2)}` : "—"}
                      </span>
                      {lastPrice && <span className="text-tape-dim"> @ {lastPrice.date}</span>}
                      <div className="text-tape-faint mt-1.5">
                        yfinance returned no intraday bars for this ticker — today&apos;s action
                        lives in the LIVE INTRADAY density below. Pick 1W+ for daily candles.
                      </div>
                    </div>
                  ) : (
                    <PriceChart price={priceView} mode={chartMode} />
                  )}
                </>
              );
            })()}

            {/* ATTENTION (daily series — hidden on 1D where it would be a single point) */}
            {!isIntradayTf && (
            <div className="mt-4 border-t border-tape-border-soft pt-3">
              <TickerChart attention={attentionView} />
              <div className="mt-1 flex flex-wrap gap-x-5 gap-y-1 tape-mono text-[10.5px] text-tape-faint px-2">
                <span>
                  <span className="inline-block w-2 h-2 rounded-sm mr-1 align-middle" style={{ background: "#4fd1c5" }} />
                  social vol
                </span>
                <span>
                  <span className="inline-block w-2 h-2 rounded-sm mr-1 align-middle" style={{ background: "#3e4656" }} />
                  structured vol
                </span>
                <span>
                  <span className="inline-block w-2 h-2 rounded-sm mr-1 align-middle" style={{ background: "#f0b44a" }} />
                  buzz-z
                </span>
                {lastAtt && (
                  <span className="ml-auto text-tape-dim">
                    latest day: {lastAtt.struct} struct · {lastAtt.social} social ·{" "}
                    sent {lastAtt.sentiment == null ? "—" : lastAtt.sentiment.toFixed(2)}
                  </span>
                )}
              </div>
            </div>
            )}

            {/* unified live chart: minute candles + our features, one time axis */}
            <div className="mt-5 border-t border-tape-border-soft pt-3">
              <LiveUnifiedChart ticker={ticker} />
            </div>

            {/* live intraday density */}
            <div className="mt-5 border-t border-tape-border-soft pt-3">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-2 mb-1.5 tape-mono text-[10.5px]">
                <span className="text-tape-muted tracking-[0.1em] font-semibold">INTRADAY · live</span>
                <div className="flex gap-1">
                  {[24, 48].map((h) => (
                    <button
                      key={h}
                      onClick={() => setIntradayHours(h)}
                      className={`px-2 py-0.5 rounded ${
                        intradayHours === h
                          ? "bg-tape-panel text-tape-accent border border-tape-border"
                          : "text-tape-faint hover:text-tape-sub border border-transparent"
                      }`}
                    >
                      {h}h
                    </button>
                  ))}
                </div>
                {/* 24h / 48h message totals — toggle-independent; moved OUT of the
                    SVG so tall bars can never cover it (the reported overlap). */}
                <div className="ml-auto flex flex-wrap items-center justify-end gap-x-3 gap-y-0.5 tabular-nums">
                  {msgTotals && (
                    <>
                      <span className="text-tape-faint">
                        24h{" "}
                        <span className="text-tape-text font-semibold">
                          {msgTotals.h24.struct + msgTotals.h24.social}
                        </span>
                        <span className="text-tape-dim">
                          {" "}
                          ({msgTotals.h24.struct}n·{msgTotals.h24.social}s)
                        </span>
                      </span>
                      <span className="text-tape-faint">
                        48h{" "}
                        <span className="text-tape-text font-semibold">
                          {msgTotals.h48.struct + msgTotals.h48.social}
                        </span>
                        <span className="text-tape-dim">
                          {" "}
                          ({msgTotals.h48.struct}n·{msgTotals.h48.social}s)
                        </span>
                      </span>
                    </>
                  )}
                  {liveNow != null && (
                    <span className="text-tape-bull">● LIVE {liveNow}/hr</span>
                  )}
                </div>
              </div>
              <IntradayDensity buckets={buckets} />
            </div>

            {/* hourly search interest (Google Trends, own-term relative 0-100 — NOT
                counts). On-demand for this ticker; honest empty label if Trends
                balks. Shares the 24/48h window with the density panel above. */}
            <div className="mt-5 border-t border-tape-border-soft pt-3">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-2 mb-1.5 tape-mono text-[10.5px]">
                <span className="text-tape-muted tracking-[0.1em] font-semibold">
                  SEARCH INTEREST · hourly
                </span>
                <span className="text-tape-dim">
                  Google Trends · {searchHourly?.label ?? "relative interest (0-100, own-term)"} —
                  not search counts
                </span>
                {searchHourly?.source === "google_trends" && sBuckets.length > 0 && (
                  <span className="ml-auto flex flex-wrap items-center justify-end gap-x-3 gap-y-0.5 tabular-nums">
                    <span className="text-tape-faint">
                      peak{" "}
                      <span className="text-tape-text font-semibold">
                        {Math.round(Math.max(...sBuckets.map((b) => b.value)))}
                      </span>
                    </span>
                    <span className="text-tape-faint">
                      latest{" "}
                      <span className="text-tape-text font-semibold">
                        {Math.round(sBuckets[sBuckets.length - 1].value)}
                      </span>
                    </span>
                  </span>
                )}
              </div>
              {searchHourly === null ? (
                <div className="px-2 py-6 tape-mono text-[10.5px] text-tape-muted text-center">
                  fetching search interest…
                </div>
              ) : (
                <SearchInterestPanel buckets={sBuckets} />
              )}
            </div>

            {/* associated news clusters */}
            <div className="mt-5 border-t border-tape-border-soft pt-3">
              <div className="px-2 mb-1 tape-mono text-[10.5px]">
                <span className="text-tape-muted tracking-[0.1em] font-semibold">ASSOCIATED NEWS</span>
                <span className="text-tape-dim ml-2">
                  {clustersView.length} clusters attributed to {ticker} in the last {tf}
                </span>
              </div>
              {clustersView.length === 0 ? (
                <div className="px-2 py-3 tape-mono text-[10.5px] text-tape-dim">
                  no news clusters attributed to this ticker in the last {tf}
                  {clusters.length > 0 && ` (${clusters.length} older — widen the timeframe)`}
                </div>
              ) : (
                <table className="w-full border-collapse tape-mono text-[11px]">
                  <tbody>
                    {clustersView.map((c) => (
                      <tr
                        key={c.cluster_id}
                        className="border-b border-tape-border-soft hover:bg-tape-panel-2 align-top"
                      >
                        <td className="px-3 py-2 text-tape-faint whitespace-nowrap w-24">
                          {c.published_at.slice(0, 10)}
                        </td>
                        <td className="px-2 py-2 w-16">
                          <span className={c.source_class === "social" ? "text-tape-accent" : "text-tape-dim"}>
                            {c.source_class === "social" ? "social" : "news"}
                          </span>
                        </td>
                        <td className="px-2 py-2 text-tape-sub leading-snug">
                          {c.url ? (
                            <a
                              href={c.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="hover:text-tape-text hover:underline decoration-tape-dim"
                            >
                              {c.title ?? "(untitled)"}
                            </a>
                          ) : (
                            (c.title ?? "(untitled)")
                          )}
                          <span className="text-tape-dim ml-2">· {c.source}</span>
                          {c.member_count > 1 && <span className="text-tape-faint ml-1">×{c.member_count}</span>}
                        </td>
                        <td className="px-2 py-2 w-28 text-right">
                          {c.catalyst_type && (
                            <span className="text-tape-warn text-[10px]">{c.catalyst_type}</span>
                          )}
                          {c.high_alert && <span className="text-tape-bear text-[10px] ml-1">⚠</span>}
                          {c.catalyst_type && c.called_at && (
                            <div
                              className="text-tape-dim text-[9.5px] mt-0.5"
                              title="when the system scored/classified this cluster"
                            >
                              called {fmtET(c.called_at)}
                            </div>
                          )}
                        </td>
                        <td
                          className={`px-3 py-2 w-16 text-right tabular-nums whitespace-nowrap ${toneColor(
                            c.finbert_score,
                          )}`}
                        >
                          {toneTag(c.finbert_score)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {/* DEEP DIVE — single-ticker AI analysis over our own data */}
            <div className="mt-5 border-t border-tape-border-soft pt-3">
              <DeepDive ticker={ticker} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
