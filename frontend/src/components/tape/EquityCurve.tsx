"use client";

import { useEffect, useRef, useState } from "react";
import type { IChartApi, UTCTimestamp } from "lightweight-charts";
import type { EquityPoint } from "@/lib/trader";
import { isChunkError, reloadOnceForStaleChunk } from "@/lib/chunkGuard";

/**
 * Account equity curve (lightweight-charts line + baseline fill). Mirrors
 * PriceChart's SSR-safe dynamic-import pattern. The line is teal; the baseline
 * (starting equity) is drawn as a faint reference so gains/losses read at a
 * glance. A fresh paper account returns one/flat point — the parent renders an
 * empty state before mounting this, so here we can assume >= 2 points.
 */

const ACCENT = "#4fd1c5";
const BULL = "#34d399";
const BEAR = "#fb7185";

export default function EquityCurve({
  points,
  height = 260,
}: {
  points: EquityPoint[];
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || points.length < 2) return;
    let chart: IChartApi | undefined;
    let onResize: (() => void) | undefined;
    let disposed = false;

    import("lightweight-charts")
      .then((LWC) => {
        if (disposed || !ref.current) return;
        const { createChart, BaselineSeries, ColorType, CrosshairMode } = LWC;
        const rows = points
          .filter((p) => p.equity != null)
          .map((p) => ({ time: p.t as UTCTimestamp, value: p.equity as number }));
        if (rows.length < 2) return;
        const base = rows[0].value;

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
          timeScale: { borderColor: "#1c2230", rightOffset: 3, timeVisible: true, secondsVisible: false },
          rightPriceScale: { borderColor: "#1c2230" },
          localization: { priceFormatter: (p: number) => "$" + p.toLocaleString("en-US", { maximumFractionDigits: 0 }) },
          crosshair: { mode: CrosshairMode.Normal },
        });

        // Baseline series colors above/below the starting equity green/red.
        const s = chart.addSeries(BaselineSeries, {
          baseValue: { type: "price", price: base },
          topLineColor: BULL,
          topFillColor1: "rgba(52,211,153,0.20)",
          topFillColor2: "rgba(52,211,153,0.02)",
          bottomLineColor: BEAR,
          bottomFillColor1: "rgba(251,113,133,0.02)",
          bottomFillColor2: "rgba(251,113,133,0.20)",
          lineWidth: 2,
        });
        s.priceScale().applyOptions({ scaleMargins: { top: 0.12, bottom: 0.12 } });
        s.setData(rows);
        chart.timeScale().fitContent();

        onResize = () => {
          if (ref.current && chart) chart.applyOptions({ width: ref.current.clientWidth });
        };
        window.addEventListener("resize", onResize);
      })
      .catch((err) => {
        if (isChunkError(err) && reloadOnceForStaleChunk()) return;
        if (!disposed) setLoadFailed(true);
      });

    return () => {
      disposed = true;
      if (onResize) window.removeEventListener("resize", onResize);
      if (chart) chart.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, height]);

  // ACCENT is referenced so an all-flat account (baseline == line) still has a
  // visible tint even when top/bottom fills collapse.
  void ACCENT;

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
  return <div ref={ref} className="w-full" />;
}
