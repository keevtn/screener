"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import HealthStrip from "@/components/tape/HealthStrip";
import TickerDetailBody from "@/components/tape/TickerDetailBody";

/**
 * TAPE_ ticker detail — full page. The same TickerDetailBody the pop-up modal
 * shows (price/attention/intraday/news/deep-dive), wrapped in terminal chrome.
 */
export default function TickerDetailPage() {
  const params = useParams();
  const ticker = String(params.ticker || "").toUpperCase();
  const clock = useClock();

  return (
    <>
      <TapeNav active="SCREENER" clock={clock} />
      <div className="shrink-0 px-[22px] py-2 border-b border-tape-border-soft bg-tape-panel-2 tape-mono text-[10.5px]">
        <Link href="/screener" className="text-tape-faint hover:text-tape-accent">
          ‹ back to screener
        </Link>
      </div>
      <TickerDetailBody ticker={ticker} />
      <HealthStrip note="ticker series :8001 · price = parquet cache · attention = legacy + live pipeline" />
    </>
  );
}
