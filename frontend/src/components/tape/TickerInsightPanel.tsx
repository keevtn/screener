"use client";

import { useEffect, useState, type ReactNode } from "react";
import { fetchScreenerStats, type TickerStats } from "@/lib/tape/screenerStats";
import { fetchUniverseScreen, fmtMcap, type UniverseRow } from "@/lib/universe";
import { ratingOf, sentPhrase, type Tone } from "@/lib/tape/insight";
import { fmtET } from "@/lib/tape/time";
import type { ClusterItem } from "@/lib/ticker";

/**
 * Single-ticker INSIGHT band (modal + /ticker/[t] page): the live numbers next
 * to the name — current rating with raw sentiment, today's mentions with the
 * news/social split, today vs the ticker's OWN history (× normal, sentiment z
 * with a plain-language read), buzz baseline, latest signal, next earnings,
 * last catalyst. All real: /screener/stats + /universe/screen + the already-
 * fetched clusters; anything without enough history renders an honest "—".
 */

const TONE_CLS: Record<Tone, string> = {
  bull: "text-tape-bull",
  bear: "text-tape-bear",
  muted: "text-tape-muted",
};

const signCls = (v: number) =>
  v > 0 ? "text-tape-bull" : v < 0 ? "text-tape-bear" : "text-tape-muted";

function Chunk({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="inline-flex items-baseline gap-1.5 whitespace-nowrap">
      <span className="text-tape-dim tracking-[0.1em] text-[9px] font-semibold">{label}</span>
      {children}
    </span>
  );
}

export default function TickerInsightPanel({
  ticker,
  clusters,
}: {
  ticker: string;
  clusters: ClusterItem[];
}) {
  const [s, setS] = useState<TickerStats | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [uni, setUni] = useState<UniverseRow | null>(null);

  // vs-own-history stats — refreshes on the same 20s cadence as the rest of the body.
  useEffect(() => {
    let cancelled = false;
    setS(null);
    setLoaded(false);
    const pull = () =>
      fetchScreenerStats([ticker]).then((m) => {
        if (!cancelled) {
          setS(m[ticker] ?? null);
          setLoaded(true);
        }
      });
    pull();
    const t = setInterval(pull, 20_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [ticker]);

  // Fundamentals row (name/sector/mcap) + latest signal + next earnings — the
  // exact-symbol-first search guarantees the ticker lands first if snapshotted.
  useEffect(() => {
    let cancelled = false;
    setUni(null);
    fetchUniverseScreen({ q: ticker, limit: 1 }).then((r) => {
      const row = r.items[0];
      if (!cancelled) setUni(row && row.ticker === ticker ? row : null);
    });
    return () => {
      cancelled = true;
    };
  }, [ticker]);

  // Most recent cluster carrying a catalyst tag (clusters arrive newest-first).
  const cat = clusters.find((c) => c.catalyst_type);
  const catAgeH = cat ? Math.max(0, (Date.now() - Date.parse(cat.published_at)) / 3.6e6) : null;
  const rating = s?.sent_today != null ? ratingOf(s.sent_today) : null;

  return (
    <div className="px-4 py-2.5 border-b border-tape-border-soft bg-[rgba(79,209,197,0.03)] tape-mono text-[10.5px] flex flex-wrap items-center gap-x-6 gap-y-1.5">
      {!loaded ? (
        <span className="text-tape-dim">loading live stats…</span>
      ) : (
        <>
          <Chunk label="RATING">
            {rating && s?.sent_today != null ? (
              <>
                <span className={`${TONE_CLS[rating.tone]} font-bold`}>{rating.txt}</span>
                <span className={signCls(s.sent_today)}>
                  {s.sent_today >= 0 ? "+" : ""}
                  {s.sent_today.toFixed(2)}
                </span>
              </>
            ) : (
              <span className="text-tape-faint">— no scored mentions today</span>
            )}
          </Chunk>
          <Chunk label="MENTIONS TODAY">
            {s ? (
              <span className="text-tape-text font-bold">
                {s.mentions_today}
                <span className="text-tape-faint font-medium">
                  {" "}
                  ({s.struct_today} news + {s.social_today} social)
                </span>
              </span>
            ) : (
              <span className="text-tape-faint">—</span>
            )}
          </Chunk>
          <Chunk label="VS SELF">
            {s && s.mentions_x_normal != null ? (
              <>
                <span
                  className={`font-bold ${
                    s.mentions_x_normal >= 2 ? "text-tape-warn" : "text-tape-text"
                  }`}
                >
                  {s.mentions_x_normal.toFixed(1)}× normal
                </span>
                <span className="text-tape-faint">
                  (avg {s.avg_daily_mentions?.toFixed(1)}/d over {s.n_days}d)
                </span>
              </>
            ) : (
              <span className="text-tape-faint">
                — insufficient history (n={s?.n_days ?? 0}d, needs ≥5)
              </span>
            )}
          </Chunk>
          <Chunk label="SENT VS SELF">
            {s && s.sent_z != null ? (
              <>
                <span className={`font-bold ${signCls(s.sent_z)}`}>
                  z {s.sent_z >= 0 ? "+" : ""}
                  {s.sent_z.toFixed(1)}
                </span>
                <span className="text-tape-sub">{sentPhrase(s.sent_z)}</span>
                <span className="text-tape-faint">
                  (today {s.sent_today?.toFixed(2)} vs own {s.sent_hist_mean?.toFixed(2)}±
                  {s.sent_hist_std?.toFixed(2)})
                </span>
              </>
            ) : (
              <span className="text-tape-faint">—</span>
            )}
          </Chunk>
          <Chunk label="BUZZ BASE">
            {s?.buzz_baseline ? (
              <span className="text-tape-sub">
                μ {s.buzz_baseline.mean.toFixed(1)}±{s.buzz_baseline.std.toFixed(1)}/d
                <span className="text-tape-faint">
                  {" "}
                  (n={s.buzz_baseline.n_days} · {s.buzz_baseline.source})
                </span>
              </span>
            ) : (
              <span className="text-tape-faint">—</span>
            )}
          </Chunk>
          {/* Google-Trends search attention vs the ticker's OWN daily history. The
              raw 0-100 index is own-normalized (not cross-ticker comparable), so the
              honest anomaly is the daily z — shown here next to buzz. Hourly detail
              lives in the SEARCH INTEREST panel; there is no per-hour z (too noisy
              for a stable intra-day baseline). */}
          <Chunk label="SEARCH VS SELF">
            {s && s.search_z != null ? (
              <>
                <span
                  className={`font-bold ${s.search_z >= 2 ? "text-tape-warn" : "text-tape-text"}`}
                  title="Google search interest today vs this ticker's own daily history (own-term 0-100 index)"
                >
                  z {s.search_z >= 0 ? "+" : ""}
                  {s.search_z.toFixed(1)}
                </span>
                <span className="text-tape-faint">
                  (today {s.search_today ?? "—"}/100 · {s.search_days}d)
                </span>
              </>
            ) : (
              <span className="text-tape-faint">
                —{" "}
                {s && s.search_days > 0
                  ? `building (${s.search_days}d, needs ≥10)`
                  : "no search baseline yet"}
              </span>
            )}
          </Chunk>
          <Chunk label="SIGNAL">
            {uni?.signal ? (
              <span className={uni.signal.direction === "bullish" ? "text-tape-bull" : "text-tape-bear"}>
                {uni.signal.direction === "bullish" ? "▲" : "▼"}{" "}
                {uni.signal.direction.toUpperCase()} {uni.signal.confidence.toFixed(2)}
              </span>
            ) : (
              <span className="text-tape-faint">—</span>
            )}
          </Chunk>
          <Chunk label="NEXT EARNINGS">
            <span className={uni?.next_earnings ? "text-tape-warn" : "text-tape-faint"}>
              {uni?.next_earnings ?? "—"}
            </span>
          </Chunk>
          <Chunk label="LAST CATALYST">
            {cat && catAgeH != null ? (
              <span className="text-tape-sub">
                {cat.catalyst_type}
                <span className="text-tape-faint">
                  {" "}
                  · {catAgeH >= 48 ? `${(catAgeH / 24).toFixed(0)}d` : `${catAgeH.toFixed(0)}h`} ago
                  {cat.called_at && <> · called {fmtET(cat.called_at)}</>}
                </span>
              </span>
            ) : (
              <span className="text-tape-faint">—</span>
            )}
          </Chunk>
          {uni && (
            <span className="text-tape-faint ml-auto">
              {uni.name ? `${uni.name.slice(0, 26)} · ` : ""}
              {uni.sector ?? "—"} · {fmtMcap(uni.market_cap)}
            </span>
          )}
        </>
      )}
    </div>
  );
}
