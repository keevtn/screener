"use client";

import { useEffect, useRef, useState } from "react";
import type { IChartApi, UTCTimestamp } from "lightweight-charts";
import type { IntradayBar, PricePoint } from "@/lib/ticker";
import { isChunkError, reloadOnceForStaleChunk } from "@/lib/chunkGuard";

/**
 * Price chart with REAL axes (via lightweight-charts): a $-formatted price scale,
 * dated time axis, candlestick or line, and a volume histogram. lightweight-charts
 * is imported inside the effect so it never runs during SSR.
 *
 * `intraday` bars (real yfinance 1m/5m/15m, ET-shifted epochs) take precedence
 * over the daily `price` series: the time axis shows clock times and pre/after-
 * hours candles render DIMMED so extended-session prints are never passed off
 * as regular-session action.
 */

const UP = "#34d399";
const DOWN = "#fb7185";
// Extended-hours (pre/post) candles: same direction encoding, dimmed.
const UP_EXT = "#1d5c46";
const DOWN_EXT = "#7c3b49";
const WICK_EXT = "#3a4254";

export default function PriceChart({
  price,
  mode,
  height = 300,
  intraday = null,
}: {
  price: PricePoint[];
  mode: "candle" | "line";
  height?: number;
  /** Real intraday bars; when present they replace the daily series. */
  intraday?: IntradayBar[] | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const useIntraday = !!intraday && intraday.length > 0;

  useEffect(() => {
    const el = ref.current;
    if (!el || (useIntraday ? intraday!.length === 0 : price.length === 0)) return;
    let chart: IChartApi | undefined;
    let onResize: (() => void) | undefined;
    let disposed = false;

    import("lightweight-charts").then((LWC) => {
      if (disposed || !ref.current) return;
      const { createChart, CandlestickSeries, LineSeries, HistogramSeries, ColorType, CrosshairMode } =
        LWC;
      chart = createChart(el, {
        width: el.clientWidth,
        height,
        layout: {
          background: { type: ColorType.Solid, color: "#0b0d12" },
          textColor: "#b9c0cf",
          fontSize: 10,
          fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
        },
        grid: { vertLines: { color: "#161a24" }, horzLines: { color: "#161a24" } },
        timeScale: {
          borderColor: "#1c2230",
          rightOffset: 3,
          timeVisible: useIntraday, // clock times on the intraday axis
          secondsVisible: false,
        },
        rightPriceScale: { borderColor: "#1c2230" },
        localization: { priceFormatter: (p: number) => "$" + p.toFixed(2) },
        crosshair: { mode: CrosshairMode.Normal },
      });

      // Unified bar shape: daily rows keyed by date string, intraday by epoch.
      const rows = useIntraday
        ? intraday!.map((b) => ({
            time: b.time as UTCTimestamp,
            open: b.open,
            high: b.high,
            low: b.low,
            close: b.close,
            volume: b.volume,
            extended: b.extended,
          }))
        : price.map((p) => ({
            time: p.date as unknown as UTCTimestamp,
            open: p.open,
            high: p.high,
            low: p.low,
            close: p.close,
            volume: p.volume,
            extended: false,
          }));

      if (mode === "candle") {
        const s = chart.addSeries(CandlestickSeries, {
          upColor: UP,
          downColor: DOWN,
          borderUpColor: UP,
          borderDownColor: DOWN,
          wickUpColor: UP,
          wickDownColor: DOWN,
        });
        s.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.28 } });
        s.setData(
          rows.map((r) => ({
            time: r.time,
            open: r.open,
            high: r.high,
            low: r.low,
            close: r.close,
            // Pre/after-hours prints render dimmed — same direction encoding.
            ...(r.extended
              ? {
                  color: r.close >= r.open ? UP_EXT : DOWN_EXT,
                  borderColor: r.close >= r.open ? UP_EXT : DOWN_EXT,
                  wickColor: WICK_EXT,
                }
              : {}),
          })),
        );
      } else {
        const s = chart.addSeries(LineSeries, { color: "#4fd1c5", lineWidth: 2 });
        s.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.28 } });
        s.setData(rows.map((r) => ({ time: r.time, value: r.close })));
      }

      const vol = chart.addSeries(HistogramSeries, {
        priceFormat: { type: "volume" },
        priceScaleId: "",
      });
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      vol.setData(
        rows.map((r) => ({
          time: r.time,
          value: r.volume ?? 0,
          color: r.extended ? WICK_EXT + "55" : r.close >= r.open ? UP + "55" : DOWN + "55",
        })),
      );

      chart.timeScale().fitContent();
      onResize = () => {
        if (ref.current && chart) chart.applyOptions({ width: ref.current.clientWidth });
      };
      window.addEventListener("resize", onResize);
    }).catch((err) => {
      // Stale webpack chunk after a dev-server recycle: a reload always fixes it.
      // Anything else degrades to an in-panel message instead of an uncaught
      // rejection (which previously took the whole page down).
      if (isChunkError(err) && reloadOnceForStaleChunk()) return;
      if (!disposed) setLoadFailed(true);
    });

    return () => {
      disposed = true;
      if (onResize) window.removeEventListener("resize", onResize);
      if (chart) chart.remove();
    };
  }, [price, mode, height, intraday, useIntraday]);

  if (loadFailed) {
    return (
      <div className="tape-mono text-[11px] text-tape-muted px-4 py-10 text-center">
        chart library failed to load —{" "}
        <button onClick={() => window.location.reload()} className="text-tape-accent hover:underline">
          reload
        </button>
      </div>
    );
  }
  if (!useIntraday && price.length === 0) {
    return (
      <div className="tape-mono text-[11px] text-tape-muted px-4 py-10 text-center">
        no cached price bars for this ticker
      </div>
    );
  }
  return <div ref={ref} className="w-full" />;
}
