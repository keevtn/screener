"use client";

import { useEffect, useRef, useState } from "react";
import type { IChartApi, ISeriesApi } from "lightweight-charts";
import { PRED_API } from "@/lib/config";
import { fetchNewsForTicker } from "@/lib/api";
import { attnSentiment } from "@/lib/buzz";
import { fetchTickerClusters } from "@/lib/ticker";
import { NEWS_EVENTS, PIPE_EVENTS, subscribeEvents, type TapeEvent } from "@/lib/events";
import { watchLive, type QuoteMap } from "@/lib/live";
import { fetchOverlay, type OverlayResult } from "@/lib/trader";
import { isChunkError, reloadOnceForStaleChunk } from "@/lib/chunkGuard";
import type { NewsItem } from "@/types/news";

/**
 * UNIFIED · LIVE — one time axis, two panes:
 *   pane 0: 1-minute price candles incl. pre/post-market (Alpaca IEX, /sim/bars)
 *   pane 1: OUR features — mentions/hour histogram + hourly mean sentiment line
 * The last candle ticks in real time off the `quotes` SSE push; `news` events
 * refresh the feature pane; bars re-poll every 60s as fallback. Degrades to an
 * honest notice when Alpaca keys are absent or the name has no IEX prints.
 */

const UP = "#34d399";
const DOWN = "#fb7185";
const WARN = "#f0b44a";
const MUTED = "#5a6478";
const ISENT = "#7aa2ff"; // integrated attention·sentiment line (iSent)

/** Format a UNIX-seconds timestamp in US Eastern (the market's clock). */
function etTime(t: number): string {
  return new Date(t * 1000).toLocaleTimeString("en-US", {
    timeZone: "America/New_York",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

function etFull(t: number): string {
  const d = new Date(t * 1000);
  const day = d.toLocaleDateString("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
  });
  return `${day} · ${etTime(t)} ET`;
}

interface SimBar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

async function fetchSimBars(ticker: string, hours = 13): Promise<{ live: boolean; items: SimBar[] }> {
  try {
    const res = await fetch(`${PRED_API}/tickers/${encodeURIComponent(ticker)}/sim/bars?hours=${hours}`, {
      cache: "no-store",
    });
    if (!res.ok) return { live: false, items: [] };
    const b = await res.json();
    return { live: !!b.live, items: b.items ?? [] };
  } catch {
    return { live: false, items: [] };
  }
}

/** Hourly mentions + mean sentiment from the ticker's news window (client-side). */
function hourlyFeatures(items: NewsItem[], hours: number) {
  const HR = 3_600_000;
  const now = Date.now();
  const start = Math.floor((now - hours * HR) / HR) * HR;
  const buckets = new Map<number, { n: number; sSum: number; sN: number }>();
  for (const it of items) {
    const t = Date.parse(it.published_at);
    if (Number.isNaN(t) || t < start) continue;
    const hk = Math.floor(t / HR) * HR;
    const b = buckets.get(hk) ?? { n: 0, sSum: 0, sN: 0 };
    b.n += 1;
    if (it.sentiment?.score != null) {
      b.sSum += it.sentiment.score;
      b.sN += 1;
    }
    buckets.set(hk, b);
  }
  const mentions: { time: number; value: number }[] = [];
  const sentiment: { time: number; value: number }[] = [];
  const isent: { time: number; value: number }[] = [];
  for (let h = start; h <= now; h += HR) {
    const b = buckets.get(h);
    const ts = Math.floor(h / 1000);
    mentions.push({ time: ts, value: b?.n ?? 0 });
    if (b && b.sN > 0) {
      const mean = b.sSum / b.sN;
      sentiment.push({ time: ts, value: mean });
      // iSent = conviction-damped tone: the hour's mean sentiment shrunk by
      // attention (ALL mentions, scored or not — attention is attention).
      // Same skip rule as the mean line: no scored items -> no point (honest gap).
      isent.push({ time: ts, value: attnSentiment(mean, b.n) });
    }
  }
  return { mentions, sentiment, isent };
}

export default function LiveUnifiedChart({ ticker, height = 380 }: { ticker: string; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lastBarRef = useRef<{ time: number; open: number; high: number; low: number; close: number } | null>(null);
  const [state, setState] = useState<"loading" | "live" | "unavailable" | "failed">("loading");
  const [lastPush, setLastPush] = useState<string | null>(null);
  // Overlay summary for the legend: our fills' alignment + any horizon note.
  const [overlayInfo, setOverlayInfo] = useState<{
    checked: number;
    misaligned: number;
    horizon: string | null;
    advisory: string | null;
  } | null>(null);

  // build + data effect
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let disposed = false;
    let refreshTimer: ReturnType<typeof setInterval> | undefined;
    let onResize: (() => void) | undefined;
    let mentionsSeries: ISeriesApi<"Histogram"> | null = null;
    let sentimentSeries: ISeriesApi<"Line"> | null = null;
    let isentSeries: ISeriesApi<"Line"> | null = null;
    let candleTimes: number[] = [];
    // The rendered bars' [low, high] by epoch — the frontend alignment re-check
    // asserts each fill marker's price falls inside its bar (defense in depth on
    // top of the server's authoritative check).
    const candleRange = new Map<number, { low: number; high: number }>();
    let setMarkers: ((m: unknown[]) => void) | null = null;
    // Two marker sources share one series overlay: catalyst pins + our fills/intent.
    let catalystMarkers: { time: number }[] = [];
    let overlayMarkers: { time: number }[] = [];
    const renderMarkers = () => {
      if (!setMarkers) return;
      const all = [...catalystMarkers, ...overlayMarkers].sort((a, b) => a.time - b.time);
      setMarkers(all as unknown[]);
    };
    // Price lines we own (entry / fill / advisory) — cleared before each redraw
    // so a 60s refresh doesn't stack duplicates.
    let ownPriceLines: unknown[] = [];

    async function loadFeatures() {
      const news = await fetchNewsForTicker(ticker, 500);
      if (disposed) return;
      const f = hourlyFeatures(news, 13);
      mentionsSeries?.setData(f.mentions as never);
      sentimentSeries?.setData(f.sentiment as never);
      isentSeries?.setData(f.isent as never);
    }

    async function loadBars() {
      const r = await fetchSimBars(ticker);
      if (disposed) return;
      if (!r.live || r.items.length === 0) {
        setState((s) => (s === "live" ? s : "unavailable"));
        return;
      }
      const candles = r.items.map((b) => ({
        time: Math.floor(Date.parse(b.time) / 1000),
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      }));
      candlesRef.current?.setData(candles as never);
      lastBarRef.current = candles.at(-1) ?? null;
      candleTimes = candles.map((c) => c.time);
      candleRange.clear();
      for (const c of candles) candleRange.set(c.time, { low: c.low, high: c.high });
      setState("live");
    }

    // Catalyst pins: each scored cluster in the window, snapped to the latest
    // candle at/before its publish minute. ▲ bullish tone / ▼ bearish / ● neutral,
    // "!" = high alert. Legend below the chart explains the shapes.
    async function loadMarkers() {
      if (!setMarkers || candleTimes.length === 0) return;
      const clusters = await fetchTickerClusters(ticker, 40);
      if (disposed) return;
      const first = candleTimes[0];
      const markers = clusters
        .map((c) => {
          const t = Math.floor(Date.parse(c.published_at) / 1000);
          if (Number.isNaN(t) || t < first) return null;
          // snap to the latest candle <= publish time
          let snap = first;
          for (const ct of candleTimes) {
            if (ct <= t) snap = ct;
            else break;
          }
          const fb = c.finbert_score;
          const bullish = fb != null && fb > 0.15;
          const bearish = fb != null && fb < -0.15;
          return {
            time: snap,
            position: bearish ? "aboveBar" : "belowBar",
            shape: bullish ? "arrowUp" : bearish ? "arrowDown" : "circle",
            color: bullish ? UP : bearish ? DOWN : MUTED,
            text: c.high_alert ? "!" : undefined,
          };
        })
        .filter(Boolean) as { time: number }[];
      catalystMarkers = markers;
      renderMarkers();
    }

    // Our own fills (snapped to the 1-min grid server-side) + the intent layer:
    // entry price line, ADVISORY vol_stop, flatten cutoff, signal-fired time.
    async function loadOverlay() {
      if (!candlesRef.current) return;
      const ov: OverlayResult = await fetchOverlay(ticker, 13);
      if (disposed) return;
      if (!ov.configured || (ov.fill_markers.length === 0 && ov.entry_lines.length === 0)) {
        overlayMarkers = [];
        renderMarkers();
        setOverlayInfo(null);
        return;
      }

      // Frontend re-check: assert each rendered fill marker's price sits inside
      // the bar it snapped to (using the actually-rendered candle, not the
      // server's copy). A mismatch is loud (console) and visually flagged WARN.
      let feMisaligned = 0;
      const fillMarks = ov.fill_markers.map((m) => {
        const rng = candleRange.get(m.bar_time);
        const localAligned = rng ? rng.low - 1e-4 <= m.price && m.price <= rng.high + 1e-4 : m.aligned;
        if (!localAligned || !m.aligned) {
          feMisaligned += 1;
          // eslint-disable-next-line no-console
          console.warn(
            `[trader] fill marker misaligned for ${ticker}: price ${m.price} outside bar ` +
              `[${rng?.low ?? m.bar_low}, ${rng?.high ?? m.bar_high}] @ ${m.bar_time}`,
          );
        }
        const buy = m.side === "buy";
        const ok = localAligned && m.aligned;
        return {
          time: m.bar_time,
          position: buy ? "belowBar" : "aboveBar",
          shape: buy ? "arrowUp" : "arrowDown",
          color: ok ? (buy ? UP : DOWN) : WARN,
          text: `${m.kind === "entry" ? "IN" : "OUT"} ${m.qty}${ok ? "" : " ⚠"}`,
        };
      });

      // Time-axis intent markers (flatten cutoff, signal-fired).
      const timeMarks: { time: number }[] = [];
      if (ov.flatten) {
        timeMarks.push({
          time: ov.flatten.time,
          position: "aboveBar",
          shape: "square",
          color: MUTED,
          text: "FLATTEN",
        } as { time: number });
      }
      if (ov.signal) {
        timeMarks.push({
          time: ov.signal.time,
          position: "belowBar",
          shape: "circle",
          color: ISENT,
          text: "SIGNAL",
        } as { time: number });
      }
      overlayMarkers = [...fillMarks, ...timeMarks] as { time: number }[];
      renderMarkers();

      // Price lines: clear ours, then draw entry (solid), each fill (dotted),
      // advisory vol_stop (dashed, labeled ADVISORY).
      const series = candlesRef.current;
      for (const pl of ownPriceLines) {
        try {
          series.removePriceLine(pl as never);
        } catch {
          /* line may already be gone after a series rebuild */
        }
      }
      ownPriceLines = [];
      const addLine = (opts: Record<string, unknown>) => {
        try {
          ownPriceLines.push(series.createPriceLine(opts as never));
        } catch {
          /* ignore — cosmetic */
        }
      };
      for (const e of ov.entry_lines) {
        addLine({ price: e.price, color: MUTED, lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: e.label });
      }
      for (const m of ov.fill_markers) {
        const buy = m.side === "buy";
        addLine({
          price: m.price,
          color: buy ? UP : DOWN,
          lineWidth: 1,
          lineStyle: 1, // dotted
          axisLabelVisible: false,
          title: m.kind === "entry" ? "IN" : "OUT",
        });
      }
      for (const a of ov.advisory) {
        addLine({ price: a.price, color: WARN, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: a.label });
      }

      setOverlayInfo({
        checked: ov.alignment.checked,
        misaligned: Math.max(ov.alignment.misaligned, feMisaligned),
        horizon: ov.horizon?.beyond_today ? ov.horizon.label : null,
        advisory: ov.advisory[0]?.label ?? null,
      });
    }

    import("lightweight-charts")
      .then((LWC) => {
        if (disposed || !ref.current) return;
        const {
          createChart,
          CandlestickSeries,
          HistogramSeries,
          LineSeries,
          ColorType,
          CrosshairMode,
          createSeriesMarkers,
        } = LWC;
        const chart = createChart(el, {
          width: el.clientWidth,
          height,
          layout: {
            background: { type: ColorType.Solid, color: "#0b0d12" },
            textColor: "#b9c0cf",
            fontSize: 10,
            fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
            panes: { separatorColor: "#1c2230" },
          },
          grid: { vertLines: { color: "#161a24" }, horzLines: { color: "#161a24" } },
          timeScale: {
            borderColor: "#1c2230",
            timeVisible: true,
            secondsVisible: false,
            // Axis ticks in US Eastern — the market's clock, not UTC.
            tickMarkFormatter: (t: number) => etTime(t),
          },
          rightPriceScale: { borderColor: "#1c2230" },
          crosshair: { mode: CrosshairMode.Normal },
          localization: {
            priceFormatter: (p: number) => "$" + p.toFixed(2),
            timeFormatter: (t: number) => etFull(t), // crosshair tooltip in ET
          },
        });
        chartRef.current = chart;

        candlesRef.current = chart.addSeries(
          CandlestickSeries,
          {
            upColor: UP,
            downColor: DOWN,
            borderUpColor: UP,
            borderDownColor: DOWN,
            wickUpColor: UP,
            wickDownColor: DOWN,
          },
          0,
        );
        mentionsSeries = chart.addSeries(
          HistogramSeries,
          { color: "#3e4656", priceFormat: { type: "volume" } },
          1,
        );
        sentimentSeries = chart.addSeries(
          LineSeries,
          {
            color: WARN,
            lineWidth: 2,
            priceScaleId: "left",
            priceFormat: { type: "price", precision: 2, minMove: 0.01 },
          },
          1,
        );
        // iSent: attention-weighted sentiment, SAME left −1..+1 scale as the
        // mean-sentiment line (that's the point — joint reading), heavier line.
        isentSeries = chart.addSeries(
          LineSeries,
          {
            color: ISENT,
            lineWidth: 3,
            priceScaleId: "left",
            priceFormat: { type: "price", precision: 2, minMove: 0.01 },
          },
          1,
        );
        // Dashed zero baseline so bullish/bearish sentiment reads at a glance.
        sentimentSeries.createPriceLine({
          price: 0,
          color: MUTED,
          lineWidth: 1,
          lineStyle: 3, // dashed
          axisLabelVisible: false,
          title: "",
        });
        // Catalyst pins live on the price pane (created empty; filled by loadMarkers).
        const markersApi = createSeriesMarkers(candlesRef.current!, []);
        setMarkers = (m) => markersApi.setMarkers(m as never);
        try {
          chart.panes()[1]?.setHeight(Math.round(height * 0.28));
        } catch {
          /* pane sizing is cosmetic — ignore on older lib versions */
        }

        loadBars().then(() => {
          chart.timeScale().fitContent();
          loadMarkers();
          loadOverlay();
        });
        loadFeatures();
        refreshTimer = setInterval(
          () =>
            loadBars().then(() => {
              loadMarkers();
              loadOverlay();
            }),
          60_000,
        );
        onResize = () => {
          if (ref.current && chartRef.current) {
            chartRef.current.applyOptions({ width: ref.current.clientWidth });
          }
        };
        window.addEventListener("resize", onResize);

        // feature + marker refresh rides the news/fired pushes
        const refreshDerived = () => {
          loadFeatures();
          loadMarkers();
        };
        const unNews = subscribeEvents(NEWS_EVENTS, refreshDerived, ["news"]);
        const unPipe = subscribeEvents(PIPE_EVENTS, refreshDerived, ["news", "fired"]);
        const cleanupSubs = () => {
          unNews();
          unPipe();
        };
        (chart as unknown as { __cleanupSubs?: () => void }).__cleanupSubs = cleanupSubs;
      })
      .catch((err) => {
        if (isChunkError(err) && reloadOnceForStaleChunk()) return;
        if (!disposed) setState("failed");
      });

    return () => {
      disposed = true;
      if (refreshTimer) clearInterval(refreshTimer);
      if (onResize) window.removeEventListener("resize", onResize);
      const chart = chartRef.current as unknown as { __cleanupSubs?: () => void } | null;
      chart?.__cleanupSubs?.();
      chartRef.current?.remove();
      chartRef.current = null;
      candlesRef.current = null;
    };
  }, [ticker, height]);

  // live tick: quotes push -> update the last candle in place
  useEffect(() => {
    const un = subscribeEvents(
      PIPE_EVENTS,
      (e: TapeEvent) => {
        const q = (e.quotes as QuoteMap | undefined)?.[ticker.toUpperCase()];
        const last = lastBarRef.current;
        if (!q || !last || !candlesRef.current) return;
        const updated = {
          ...last,
          close: q.price,
          high: Math.max(last.high, q.price),
          low: Math.min(last.low, q.price),
        };
        lastBarRef.current = updated;
        candlesRef.current.update(updated as never);
        setLastPush(new Date().toLocaleTimeString());
      },
      ["quotes"],
    );
    // make sure the pump is watching this ticker
    watchLive([ticker]);
    const t = setInterval(() => watchLive([ticker]), 60_000);
    return () => {
      un();
      clearInterval(t);
    };
  }, [ticker]);

  return (
    <div>
      <div className="flex items-center gap-2 px-2 mb-1 tape-mono text-[10.5px]">
        <span className="text-tape-muted tracking-[0.1em] font-semibold">
          UNIFIED · LIVE <span className="text-tape-dim">1-min candles + mentions/hr + sentiment + iSent</span>
        </span>
        {state === "live" && (
          <span className="text-tape-bull">
            ● alpaca-iex{lastPush ? ` · last push ${lastPush}` : ""}
          </span>
        )}
      </div>
      {/* key: what every mark on the chart means (all times ET) */}
      {state === "live" && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-2 mb-1 tape-mono text-[9.5px] text-tape-faint">
          <span>
            <span className="inline-block w-2 h-2 mr-1 align-middle" style={{ background: UP }} />
            /
            <span className="inline-block w-2 h-2 mx-1 align-middle" style={{ background: DOWN }} />
            1-min candle up/down (incl. pre/post-mkt)
          </span>
          <span>
            <span className="inline-block w-2 h-2 mr-1 align-middle" style={{ background: "#3e4656" }} />
            mentions/hr (lower pane)
          </span>
          <span>
            <span className="inline-block w-3 h-[2px] mr-1 align-middle" style={{ background: WARN }} />
            hourly mean sentiment (−1…+1, left scale; dashed = 0)
          </span>
          <span>
            <span className="inline-block w-3 h-[3px] mr-1 align-middle" style={{ background: ISENT }} />
            iSent = sentiment × n/(n+3) (attention-weighted tone, same scale)
          </span>
          <span>
            <span style={{ color: UP }}>▲</span>/<span style={{ color: DOWN }}>▼</span>/
            <span style={{ color: MUTED }}>●</span> catalyst fired (bullish / bearish / neutral tone
            · <span className="text-tape-warn">!</span> = high alert)
          </span>
          <span>
            <span style={{ color: UP }}>▲</span>IN/<span style={{ color: DOWN }}>▼</span>OUT our fills
            (snapped to bar; price line at fill) ·{" "}
            <span style={{ color: MUTED }}>■</span>FLATTEN · <span style={{ color: ISENT }}>●</span>SIGNAL
          </span>
          {overlayInfo?.advisory && (
            <span className="text-tape-warn">- - {overlayInfo.advisory} (nothing executes off this)</span>
          )}
          {overlayInfo?.horizon && <span className="text-tape-faint">{overlayInfo.horizon}</span>}
          {overlayInfo && overlayInfo.checked > 0 && (
            <span className={overlayInfo.misaligned > 0 ? "text-tape-warn" : "text-tape-bull"}>
              fills aligned {overlayInfo.checked - overlayInfo.misaligned}/{overlayInfo.checked}
              {overlayInfo.misaligned > 0 ? " ⚠ (see console)" : ""}
            </span>
          )}
          <span className="text-tape-dim">axis: ET</span>
        </div>
      )}
      {state === "unavailable" ? (
        <div className="tape-mono text-[10.5px] text-tape-dim px-2 py-6 text-center">
          no live minute bars (Alpaca keys absent or no IEX prints for this name) — daily panels
          above remain authoritative
        </div>
      ) : state === "failed" ? (
        <div className="tape-mono text-[10.5px] text-tape-dim px-2 py-6 text-center">
          chart library failed to load —{" "}
          <button onClick={() => window.location.reload()} className="text-tape-accent hover:underline">
            reload
          </button>
        </div>
      ) : (
        <div ref={ref} className="w-full" />
      )}
    </div>
  );
}
