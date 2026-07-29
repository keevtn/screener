"use client";

import { useEffect, useMemo, useState } from "react";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import { markSeen } from "@/lib/tape/navBadges";
import { NEWS_EVENTS, subscribeEvents } from "@/lib/events";
import HealthStrip from "@/components/tape/HealthStrip";
import TapeFilterCombo, { type Suggestion } from "@/components/tape/TapeFilterCombo";
import NewsDateCalendar from "@/components/tape/NewsDateCalendar";
import { TickerButton } from "@/components/tape/TickerModalProvider";
import { fetchNews } from "@/lib/api";
import { fetchNewsArchive, fetchNewsDates, type NewsDateOption } from "@/lib/newsArchive";
import { formatDistanceToNow } from "@/lib/time";
import { toneColor, toneTag } from "@/lib/tape/insight";
import type { NewsItem } from "@/types/news";

/**
 * TAPE_ LIVE — the real-time news tape (root surface).
 *
 * The terminal's home: structured (RSS/SEC/FDA) and social feeds off the live
 * middleware (/api/news, :8000), rendered as a dense tape with sentiment, tickers,
 * and source. This replaced the old news-feed dashboard — everything is TAPE now.
 */

type Lane = "structured" | "social";

// Tone color/badge come from the shared tape-insight helpers (toneColor/toneTag)
// so the LIVE tape, catalysts, and ticker rows render tone identically.

// Some upstream feeds (e.g. FierceBiotech) embed raw HTML in the headline; strip
// tags + decode a few common entities so the tape renders clean text.
function cleanTitle(raw: string): string {
  return raw
    .replace(/<[^>]*>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&nbsp;/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .trim();
}

// Filler words dropped from headline-keyword suggestions so the dropdown ranks
// meaningful terms (company/event words) rather than glue.
const STOP = new Set([
  "the", "and", "for", "with", "from", "that", "this", "are", "was", "will", "has", "have",
  "its", "his", "her", "their", "our", "you", "your", "not", "but", "all", "new", "now",
  "after", "over", "into", "amid", "says", "said", "report", "reports", "reported", "update",
  "inc", "corp", "ltd", "plc", "co", "as", "at", "by", "in", "of", "on", "to", "up", "or",
  "a", "an", "is", "it", "be", "we", "he", "she", "they", "than", "more", "who", "what",
]);

export default function LivePage() {
  const clock = useClock();
  const [lane, setLane] = useState<Lane>("structured");
  const [items, setItems] = useState<NewsItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [reachable, setReachable] = useState(true);
  const [tickerChips, setTickerChips] = useState<string[]>([]);
  const [headlineChips, setHeadlineChips] = useState<string[]>([]);
  const [sourceChips, setSourceChips] = useState<string[]>([]);
  // Bumped by the SSE stream when the backend announces new items -> the load
  // effect below re-runs immediately (push); the 20s poll stays as fallback.
  const [pushTick, setPushTick] = useState(0);
  // null = LIVE rolling tape (default); an ISO date = browse that day's archive
  // from raw_items (SSE + poll paused, exactly one day shown).
  const [archiveDate, setArchiveDate] = useState<string | null>(null);
  const [dates, setDates] = useState<NewsDateOption[]>([]);
  const [dayTotal, setDayTotal] = useState(0); // archive: full filtered day count (for the 1000 cap note)
  const isArchive = archiveDate !== null;

  // Navigable dates for the calendar — fetched once (days that have news).
  useEffect(() => {
    fetchNewsDates().then((r) => setDates(r.dates));
  }, []);

  // Live push only in LIVE mode; the archive is a frozen day, so no stream.
  useEffect(() => {
    if (isArchive) return;
    return subscribeEvents(NEWS_EVENTS, () => setPushTick((n) => n + 1), ["news"]);
  }, [isArchive]);

  // Lane / date switches show the loading state; push-triggered refreshes must
  // NOT flash it (the tape just updates in place).
  useEffect(() => {
    setLoading(true);
  }, [lane, archiveDate]);

  useEffect(() => {
    let cancelled = false;
    async function loadLive() {
      const data = await fetchNews(1000, lane);
      if (cancelled) return;
      setItems(data);
      setReachable(data.length > 0 || reachable);
      setLoading(false);
      markSeen("LIVE"); // viewing the tape clears its "new items" nav badge
    }
    async function loadArchive(date: string) {
      const res = await fetchNewsArchive({ date, lane, limit: 1000 });
      if (cancelled) return;
      setItems(res.items);
      setDayTotal(res.count);
      setReachable(res.reachable);
      setLoading(false);
    }
    if (isArchive) {
      loadArchive(archiveDate);
      return () => {
        cancelled = true;
      };
    }
    loadLive();
    const t = setInterval(loadLive, 20_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lane, pushTick, archiveDate]);

  // Volume-ranked suggestions within the current window: tickers by mention
  // count, headline terms by how many items contain them (document frequency).
  const tickerSuggestions = useMemo<Suggestion[]>(() => {
    const counts = new Map<string, number>();
    for (const it of items) {
      for (const t of it.tickers ?? []) counts.set(t, (counts.get(t) ?? 0) + 1);
    }
    return [...counts.entries()]
      .map(([value, count]) => ({ value, count }))
      .sort((a, b) => b.count - a.count);
  }, [items]);

  // Sources ranked by item volume in the current window (e.g. Bluesky, SEC EDGAR,
  // PR Newswire, Yahoo). Values are the raw source names the SRC column shows.
  const sourceSuggestions = useMemo<Suggestion[]>(() => {
    const counts = new Map<string, number>();
    for (const it of items) {
      if (it.source) counts.set(it.source, (counts.get(it.source) ?? 0) + 1);
    }
    return [...counts.entries()]
      .map(([value, count]) => ({ value, count }))
      .sort((a, b) => b.count - a.count);
  }, [items]);

  const headlineSuggestions = useMemo<Suggestion[]>(() => {
    const counts = new Map<string, number>();
    for (const it of items) {
      const seen = new Set<string>();
      for (const w of cleanTitle(it.title).toLowerCase().split(/[^a-z0-9]+/)) {
        if (w.length < 3 || STOP.has(w) || /^\d+$/.test(w) || seen.has(w)) continue;
        seen.add(w);
        counts.set(w, (counts.get(w) ?? 0) + 1);
      }
    }
    return [...counts.entries()]
      .filter(([, c]) => c >= 2)
      .map(([value, count]) => ({ value, count }))
      .sort((a, b) => b.count - a.count);
  }, [items]);

  // Filters combine (AND across the three; OR within each set of chips).
  const rows = useMemo(() => {
    return items.filter((it) => {
      if (tickerChips.length && !(it.tickers ?? []).some((t) => tickerChips.includes(t))) {
        return false;
      }
      if (headlineChips.length) {
        const title = cleanTitle(it.title).toLowerCase();
        if (!headlineChips.some((k) => title.includes(k.toLowerCase()))) return false;
      }
      if (sourceChips.length) {
        const src = (it.source ?? "").toLowerCase();
        if (!sourceChips.some((s) => s.toLowerCase() === src)) return false;
      }
      return true;
    });
  }, [items, tickerChips, headlineChips, sourceChips]);

  return (
    <>
      <TapeNav active="LIVE" clock={clock} />

      {/* control strip */}
      <div className="shrink-0 flex flex-wrap items-center gap-x-4 gap-y-2 px-[22px] py-3 border-b border-tape-border bg-tape-panel-2 tape-mono text-[11px]">
        {isArchive ? (
          <span className="flex items-center gap-1.5 text-tape-warn tracking-[0.12em] font-semibold">
            <span className="w-[6px] h-[6px] rounded-full bg-tape-warn" aria-hidden />
            ARCHIVE
            <span className="text-tape-sub">
              — {dates.find((d) => d.date === archiveDate)?.label ?? archiveDate}
            </span>
            <button
              onClick={() => setArchiveDate(null)}
              className="ml-1 text-tape-accent underline decoration-tape-dim hover:decoration-tape-accent"
            >
              back to live
            </button>
          </span>
        ) : (
          <span className="flex items-center gap-1.5 text-tape-muted tracking-[0.12em] font-semibold">
            <span className="w-[6px] h-[6px] rounded-full bg-tape-bull tape-pulse" aria-hidden />
            LIVE
          </span>
        )}
        <NewsDateCalendar dates={dates} selected={archiveDate} onSelect={setArchiveDate} />
        <div className="flex items-center gap-1">
          {(["structured", "social"] as Lane[]).map((l) => (
            <button
              key={l}
              onClick={() => setLane(l)}
              className={`px-2.5 py-1 rounded tracking-[0.08em] ${
                lane === l
                  ? "bg-tape-panel text-tape-accent border border-tape-border"
                  : "text-tape-faint hover:text-tape-sub border border-transparent"
              }`}
            >
              {l === "structured" ? "STRUCTURED" : "SOCIAL"}
            </button>
          ))}
        </div>
        <span className="text-tape-faint">
          {lane === "structured" ? "RSS · SEC · FDA" : "Reddit · StockTwits · Bluesky"}
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2">
          <TapeFilterCombo
            label="TICKER"
            placeholder="filter ticker…"
            suggestions={tickerSuggestions}
            selected={tickerChips}
            onAdd={(v) => setTickerChips((c) => (c.includes(v) ? c : [...c, v]))}
            onRemove={(v) => setTickerChips((c) => c.filter((x) => x !== v))}
            uppercase
          />
          <TapeFilterCombo
            label="HEADLINE"
            placeholder="filter keyword…"
            suggestions={headlineSuggestions}
            selected={headlineChips}
            onAdd={(v) => setHeadlineChips((c) => (c.includes(v) ? c : [...c, v]))}
            onRemove={(v) => setHeadlineChips((c) => c.filter((x) => x !== v))}
          />
          <TapeFilterCombo
            label="SOURCE"
            placeholder="filter source…"
            suggestions={sourceSuggestions}
            selected={sourceChips}
            onAdd={(v) => setSourceChips((c) => (c.includes(v) ? c : [...c, v]))}
            onRemove={(v) => setSourceChips((c) => c.filter((x) => x !== v))}
          />
        </div>
        <span className="text-tape-faint">
          <span className="text-tape-sub">{rows.length}</span> items
          {isArchive && dayTotal > items.length && (
            <span className="text-tape-warn"> · first {items.length} of {dayTotal}</span>
          )}
        </span>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted">
          loading tape…
        </div>
      ) : rows.length === 0 ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          {reachable
            ? "No items match. Clear the filter or switch lane."
            : isArchive
              ? "No archive for this day from the prediction API (:8001). Is it running?"
              : "No items from the middleware (:8000). Is it running, and has ingestion populated Mongo?"}
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
          <table className="w-full border-collapse tape-mono text-[11px]">
            <thead>
              <tr className="sticky top-0 bg-tape-panel-2 text-tape-muted text-left tracking-[0.1em] border-b border-tape-border z-10">
                <th className="px-4 py-2 font-semibold w-16">TIME</th>
                <th className="px-2 py-2 font-semibold w-28">SRC</th>
                <th className="px-2 py-2 font-semibold w-20">SENT</th>
                <th className="px-3 py-2 font-semibold">HEADLINE</th>
                <th className="px-3 py-2 font-semibold w-40">TICKERS</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => (
                <tr
                  key={item.id}
                  className="border-b border-tape-border-soft hover:bg-tape-panel-2 align-top"
                >
                  <td className="px-4 py-2 text-tape-faint whitespace-nowrap">
                    {formatDistanceToNow(item.published_at)}
                  </td>
                  <td className="px-2 py-2 text-tape-dim uppercase truncate max-w-[7rem]">
                    {item.source_type} · {item.source}
                  </td>
                  <td className={`px-2 py-2 whitespace-nowrap ${toneColor(item.sentiment?.score)}`}>
                    {toneTag(item.sentiment?.score)}
                  </td>
                  <td className="px-3 py-2 text-tape-sub leading-snug">
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="hover:text-tape-text hover:underline decoration-tape-dim"
                    >
                      {cleanTitle(item.title)}
                    </a>
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {(item.tickers ?? []).slice(0, 5).map((t) => (
                        <TickerButton
                          key={t}
                          ticker={t}
                          className="px-1.5 py-0.5 rounded border border-tape-border-soft text-tape-accent text-[10px] hover:bg-tape-panel-2"
                        />
                      ))}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <HealthStrip
        note={
          isArchive
            ? `archive · ${archiveDate} · ${lane} lane · raw_items (:8001) · live paused`
            : reachable
              ? `news middleware :8000 · ${lane} lane · auto-refresh 20s`
              : "news middleware offline :8000"
        }
      />
    </>
  );
}
