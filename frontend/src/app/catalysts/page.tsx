"use client";

import { useEffect, useState } from "react";
import TapeNav, { useClock } from "@/components/tape/TapeNav";
import HealthStrip from "@/components/tape/HealthStrip";
import { TickerButton } from "@/components/tape/TickerModalProvider";
import {
  fetchPremarket,
  loadPanelData,
  type FiredCatalyst,
  type PanelData,
  type PremarketData,
  type PremarketRow,
} from "@/lib/catalysts";
import {
  type ExtendedMovers,
  type ExtendedRow,
  fetchExtendedMovers,
  pctStr,
  streakPhrase,
} from "@/lib/extended";
import { markSeen } from "@/lib/tape/navBadges";
import { PIPE_EVENTS, subscribeEvents } from "@/lib/events";
import { toneColor, toneTag } from "@/lib/tape/insight";
import { agoShort, callLag, fmtET } from "@/lib/tape/time";

/**
 * TAPE_ Catalysts surface (docs/ROADMAP.md Phase 5b).
 *
 * Three live bands off the prediction API (:8001): the PREMARKET morning panel
 * (PMR — frozen overnight ranking, live-recomputed before the open, graded at
 * the close), the SCHEDULED calendar (forward-looking earnings from Finviz) and
 * FIRED catalysts (recent scored clusters). Presets shown are the shared filter
 * objects the screener + ranker also consume (I11). Read-only.
 */

// Some upstream headlines embed raw HTML (anchor tags, entities); strip to clean
// text, mirroring the LIVE tape's cleanTitle.
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

// Tooltip for a fired row's tone badge: both sentiment axes + the FinBERT label,
// so the secondary (LM) axis is available on hover without cluttering the row —
// the primary (FinBERT) is the visible badge, mirroring LIVE/INSIGHT.
function sentTooltip(c: FiredCatalyst): string {
  const parts: string[] = [];
  if (c.finbert_score != null) {
    const lbl = c.finbert_label ? ` (${c.finbert_label})` : "";
    parts.push(`FinBERT ${c.finbert_score >= 0 ? "+" : ""}${c.finbert_score.toFixed(2)}${lbl}`);
  }
  if (c.lm_score != null) parts.push(`LM ${c.lm_score >= 0 ? "+" : ""}${c.lm_score.toFixed(2)}`);
  return parts.length ? parts.join(" · ") : "no sentiment scored";
}

function daysColor(d: number | null): string {
  if (d === null) return "text-tape-faint";
  if (d <= 2) return "text-tape-bear";
  if (d <= 7) return "text-tape-warn";
  return "text-tape-sub";
}

// TAPE badge text-color per catalyst family — a few families carry a signal
// color; anything else falls back to neutral. Border stays uniform (soft).
function catColor(type: string): string {
  const t = type.toLowerCase();
  if (t.includes("fda") || t.includes("approval") || t.includes("pdufa")) return "text-tape-bull";
  if (t.includes("dilut") || t.includes("offering") || t.includes("lockup")) return "text-tape-bear";
  if (t.includes("earnings")) return "text-tape-warn";
  if (t === "ma" || t.includes("merger") || t.includes("acqui")) return "text-tape-accent";
  return "text-tape-sub";
}

// --- PREMARKET band helpers --------------------------------------------------

// ET wall-clock pieces for the premarket window logic — Intl with the tape's
// timezone (no deps, DST-correct): minutes-of-day plus a weekday flag.
const etClockFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function etClock(now: number = Date.now()): { minutes: number; weekday: boolean } {
  const parts = etClockFmt.formatToParts(new Date(now));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const wd = get("weekday");
  return {
    // hour12:false can render midnight as "24" in some engines; normalize.
    minutes: (Number(get("hour")) % 24) * 60 + Number(get("minute")),
    weekday: wd !== "Sat" && wd !== "Sun",
  };
}

/** Weekday before the 09:30 ET open — when a ?live=1 recompute is meaningful. */
function inPremarketHours(now?: number): boolean {
  const c = etClock(now);
  return c.weekday && c.minutes < 9 * 60 + 30;
}

/** After 10:00 ET (or weekends) the band defaults to collapsed — the morning
 * call is old news by then. It never disappears; the header toggle reopens it. */
function pmCollapsedByDefault(now?: number): boolean {
  const c = etClock(now);
  return !c.weekday || c.minutes >= 10 * 60;
}

// Lean chip per the panel's net directional read; "none" renders as a dim dash.
function leanChip(lean: PremarketRow["lean"]): { txt: string; cls: string } | null {
  if (lean === "long") return { txt: "LONG", cls: "text-tape-bull" };
  if (lean === "short") return { txt: "SHORT", cls: "text-tape-bear" };
  if (lean === "mixed") return { txt: "MIXED", cls: "text-tape-warn" };
  return null;
}

// Panel rows carry age at compute time (frozen) — render that, not a live "ago".
function pmAge(h: number | null): string {
  if (h == null) return "—";
  return h < 1 ? `${Math.round(h * 60)}m` : `${h.toFixed(1)}h`;
}

function pmAbsPct(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(2)}%`;
}

// Extended-session % tone (tailwind classes, matching the panel palette).
function extPctCls(v: number | null | undefined): string {
  if (v == null) return "text-tape-faint";
  return v >= 0 ? "text-tape-bull" : "text-tape-bear";
}

export default function CatalystsPage() {
  const clock = useClock();
  const [data, setData] = useState<PanelData>({
    reachable: true,
    scheduled: [],
    fired: [],
    presets: [],
  });
  const [loading, setLoading] = useState(true);
  // Bumped by SSE when new clusters fire -> instant refresh; 30s poll = fallback.
  const [pushTick, setPushTick] = useState(0);

  // PREMARKET band. pm = last fetch (null until the first lands); pmOpen = the
  // user's explicit toggle, null = follow the clock default (collapsed ≥10:00 ET).
  const [pm, setPm] = useState<PremarketData | null>(null);
  const [pmOpen, setPmOpen] = useState<boolean | null>(null);
  // Day-calendar selection for the panel. undefined = latest session (morning
  // ranking + folded extended follow-through); an ISO date navigates to a past
  // session's extended movers (the morning ranking is kept for the latest only).
  const [pmDate, setPmDate] = useState<string | undefined>(undefined);
  const [ext, setExt] = useState<ExtendedMovers | null>(null);
  // FIRED feed window (days): 1 / 2 / 7. Default 1 week — newest-first, scrollable.
  const [firedDays, setFiredDays] = useState(7);

  useEffect(
    () => subscribeEvents(PIPE_EVENTS, () => setPushTick((n) => n + 1), ["fired"]),
    [],
  );

  // PREMARKET poll — 60s, independent of the 30s panel poll. During premarket
  // hours ask for a live recompute (?live=1); otherwise the frozen snapshot.
  useEffect(() => {
    let cancelled = false;
    async function loadPm() {
      const d = await fetchPremarket(inPremarketHours());
      if (!cancelled) setPm(d);
    }
    loadPm();
    const t = setInterval(loadPm, 60_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  // Extended-session data for the selected day (default: latest). Supplies both
  // the day-calendar options (available_dates) and the pre→regular→after
  // follow-through folded into the morning panel rows. Self-contained + defensive.
  useEffect(() => {
    let cancelled = false;
    const loadExt = () => fetchExtendedMovers(pmDate).then((d) => !cancelled && setExt(d));
    loadExt();
    const t = setInterval(loadExt, 60_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [pmDate]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const d = await loadPanelData(firedDays);
      if (!cancelled) {
        setData(d);
        setLoading(false);
        markSeen("CATALYSTS"); // viewing fired catalysts clears the nav badge
      }
    }
    load();
    const t = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [pushTick, firedDays]);

  const withinWeek = data.scheduled.filter(
    (e) => e.days_until !== null && e.days_until <= 7,
  ).length;

  // Effective PREMARKET body state — an explicit toggle wins; otherwise the
  // clock default (useClock re-renders each second, so the 10:00 ET collapse
  // flips on its own without a dedicated timer).
  const pmExpanded = pmOpen ?? !pmCollapsedByDefault();

  // --- day calendar + extended follow-through (folded into the morning panel) ---
  const extDates = ext?.available_dates ?? [];
  const extMovers = ext?.movers ?? [];
  // ONE panel, two modes driven by the date strip: LIVE (pmDate === undefined, the
  // default on load) shows TODAY's live morning catalysts with auto-refresh; picking
  // any past date switches this same panel to that session's extended-movers history.
  // (Semantics are the explicit selection now — not "is the newest ext date shown",
  // which made the live panel indistinguishable from just viewing today's date.)
  const viewingPast = pmDate !== undefined;
  // ticker -> extended row, to fold pm/reg/ah into the latest-session ranking.
  const extByTicker: Record<string, ExtendedRow> = {};
  for (const r of extMovers) extByTicker[r.ticker] = r;

  return (
    <>
      <TapeNav active="CATALYSTS" clock={clock} />

      {/* summary strip */}
      <div className="shrink-0 flex flex-wrap items-center gap-x-6 gap-y-2 px-[22px] py-3 border-b border-tape-border bg-tape-panel-2 tape-mono text-[11px]">
        <span className="text-tape-muted tracking-[0.12em] font-semibold">CATALYSTS</span>
        <span className="text-tape-faint">
          scheduled <span className="text-tape-sub">{data.scheduled.length}</span>
        </span>
        <span className="text-tape-faint">
          within 7d <span className="text-tape-warn">{withinWeek}</span>
        </span>
        <span className="text-tape-faint">
          fired <span className="text-tape-sub">{data.fired.length}</span>
        </span>
        {data.presets.length > 0 && (
          <span className="text-tape-faint ml-auto">
            presets:{" "}
            {data.presets.map((p) => (
              <span
                key={p}
                className="ml-1.5 px-1.5 py-0.5 rounded border border-tape-border-soft text-tape-sub"
              >
                {p}
              </span>
            ))}
          </span>
        )}
      </div>

      {/* PREMARKET morning panel — the PMR ranking with the extended-session
          (premarket → regular → afterhours) follow-through folded into each row,
          plus a day-calendar to browse past sessions' extended movers. Height-
          capped (max-h-[45vh]) and internally scrollable so it never pushes the
          SCHEDULED / FIRED panels off-screen. Body collapses after 10:00 ET (or
          via the toggle); the header always renders once loaded. */}
      {!loading && data.reachable && pm && (
        <section className="shrink-0 flex flex-col max-h-[45vh] overflow-hidden border-b border-tape-border">
          <div className="shrink-0 flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 tape-mono text-[10px] border-b border-tape-border bg-tape-panel-2">
            <span className="text-tape-muted tracking-[0.12em]">PREMARKET · morning panel</span>
            {/* ONE selector: LIVE (default) shows today's live morning catalysts;
                any past date switches this same panel to that session's extended
                movers. LIVE is always present (even pre-market with no ext rows yet),
                and today's live session is dropped from the dated options so it never
                appears twice / ambiguously as both "LIVE" and a date. */}
            <select
              value={pmDate ?? "__LIVE__"}
              onChange={(e) =>
                setPmDate(e.target.value === "__LIVE__" ? undefined : e.target.value)
              }
              className="bg-tape-panel border border-tape-border rounded px-1.5 py-0.5 text-[10px] text-tape-text cursor-pointer"
              title="LIVE = today's morning catalysts; pick a date for a past session"
              aria-label="session date — LIVE or a past session"
            >
              <option value="__LIVE__">● LIVE — today</option>
              {extDates
                .filter((d) => d.date !== pm.session_date)
                .map((d) => (
                  <option key={d.date} value={d.date}>
                    {d.label}
                  </option>
                ))}
            </select>
            {viewingPast ? (
              <>
                <span className="px-1.5 py-0.5 rounded border border-tape-border-soft text-tape-warn">
                  past session · extended movers
                </span>
                <button
                  type="button"
                  onClick={() => setPmDate(undefined)}
                  className="text-tape-accent hover:underline"
                >
                  ← LIVE
                </button>
              </>
            ) : (
              <>
                {/* Persistent LIVE-mode marker: the panel is showing today's morning
                    catalysts and auto-refreshes (60s). Shown whether or not the server
                    did a pre-open live recompute, so post-open it never reads as "off". */}
                <span
                  className="text-tape-bull"
                  title="today's live morning catalysts — auto-refreshes every 60s"
                >
                  ● LIVE
                </span>
                {pm.computed_at && (
                  <span className="text-tape-faint">computed {fmtET(pm.computed_at)}</span>
                )}
                {pm.live && (
                  <span
                    className="text-tape-faint"
                    title="recomputed live on this fetch (premarket window open)"
                  >
                    · recomputed
                  </span>
                )}
                {pm.stale && (
                  <span
                    className="px-1.5 py-0.5 rounded border border-tape-border-soft text-tape-warn"
                    title="not this session's panel — weekend/holiday mornings serve the prior session"
                  >
                    STALE
                  </span>
                )}
                {pm.graded && (
                  <span className="text-tape-accent" title="next-close outcomes attached">
                    GRADED
                  </span>
                )}
                <span className="text-tape-faint">
                  rows <span className="text-tape-sub">{pm.count}</span>
                </span>
              </>
            )}
            <button
              type="button"
              onClick={() => setPmOpen(!pmExpanded)}
              className="ml-auto text-tape-faint hover:text-tape-accent"
            >
              {pmExpanded ? "collapse ▴" : "expand ▾"}
            </button>
          </div>

          {pmExpanded &&
            (viewingPast ? (
              /* PAST SESSION — extended movers retrospective (pre→regular→after +
                 streak). Lives inside the height-capped, scrollable panel. */
              <div className="min-h-0 overflow-y-auto">
                {!ext?.reachable ? (
                  <div className="px-4 py-3 tape-mono text-[10.5px] text-tape-faint">
                    extended movers unavailable — :8001 is offline or an older build without
                    this endpoint (restart it to enable).
                  </div>
                ) : extMovers.length === 0 ? (
                  <div className="px-4 py-3 tape-mono text-[10.5px] text-tape-faint">
                    no premarket movers logged for {ext?.date}.
                  </div>
                ) : (
                  <table className="w-full border-collapse tape-mono text-[11px]">
                    <thead>
                      <tr className="text-tape-muted text-left text-[10px] tracking-[0.1em] border-b border-tape-border">
                        <th className="px-4 py-1.5 font-semibold w-20">TICKER</th>
                        <th className="px-2 py-1.5 font-semibold w-20">PRE-MKT</th>
                        <th className="px-2 py-1.5 font-semibold w-20">REGULAR</th>
                        <th className="px-2 py-1.5 font-semibold w-20">AFTER-HR</th>
                        <th className="px-2 py-1.5 font-semibold">DAY-OVER-DAY</th>
                      </tr>
                    </thead>
                    <tbody>
                      {extMovers.map((r) => {
                        const phrase = streakPhrase(r.premarket_streak);
                        const gain = r.premarket_streak?.direction === "gain";
                        return (
                          <tr
                            key={r.ticker}
                            className="border-b border-tape-border-soft hover:bg-tape-panel-2"
                          >
                            <td className="px-4 py-1.5">
                              <TickerButton
                                ticker={r.ticker}
                                className="text-tape-text font-semibold hover:text-tape-accent"
                              />
                            </td>
                            <td className={`px-2 py-1.5 tabular-nums font-semibold ${extPctCls(r.pm_pct)}`}>
                              {pctStr(r.pm_pct)}
                            </td>
                            <td className={`px-2 py-1.5 tabular-nums ${extPctCls(r.reg_pct)}`}>
                              {pctStr(r.reg_pct)}
                            </td>
                            <td className={`px-2 py-1.5 tabular-nums ${extPctCls(r.ah_pct)}`}>
                              {pctStr(r.ah_pct)}
                            </td>
                            <td className="px-2 py-1.5 text-[10px]">
                              {phrase ? (
                                <span className={gain ? "text-tape-bull" : "text-tape-bear"}>
                                  {phrase}
                                </span>
                              ) : (
                                <span className="text-tape-faint">—</span>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            ) : !pm.reachable ? (
              <div className="px-4 py-3 tape-mono text-[10.5px] text-tape-faint">
                premarket endpoint unreachable
              </div>
            ) : !pm.available ? (
              <div className="px-4 py-3 tape-mono text-[10.5px] text-tape-faint">
                no premarket panel yet — scripts/run_pipeline.py freezes one before the open
              </div>
            ) : (
              <div className="min-h-0 overflow-y-auto">
                {pm.graded && pm.summary && (
                  <div className="px-4 py-2 tape-mono text-[10.5px] text-tape-faint border-b border-tape-border-soft">
                    graded{" "}
                    <span className="text-tape-sub">
                      {pm.summary.graded_n}/{pm.summary.total}
                    </span>
                    {" · top5 mean |o→c| "}
                    <span className="text-tape-sub">{pmAbsPct(pm.summary.top5_mean_abs_oc)}</span>
                    {" vs rest "}
                    <span className="text-tape-sub">{pmAbsPct(pm.summary.rest_mean_abs_oc)}</span>
                    {" · lean hit "}
                    <span className="text-tape-sub">
                      {pm.summary.lean_hit_rate == null
                        ? "—"
                        : `${(pm.summary.lean_hit_rate * 100).toFixed(0)}%`}
                    </span>
                  </div>
                )}
                {pm.rows.length === 0 ? (
                  <div className="px-4 py-3 tape-mono text-[10.5px] text-tape-faint">
                    panel is empty — no overnight candidates ranked
                  </div>
                ) : (
                  <table className="w-full border-collapse tape-mono text-[11px]">
                    <thead>
                      <tr className="text-tape-muted text-left text-[10px] tracking-[0.1em] border-b border-tape-border">
                        <th className="px-4 py-1.5 font-semibold w-8">#</th>
                        <th className="px-2 py-1.5 font-semibold w-20">TICKER</th>
                        <th className="px-2 py-1.5 font-semibold">TOP CATALYST</th>
                        <th className="px-2 py-1.5 font-semibold">TYPE · STAGE</th>
                        <th
                          className="px-2 py-1.5 font-semibold w-14"
                          title="materiality of the top catalyst (0–1)"
                        >
                          MAT
                        </th>
                        <th
                          className="px-2 py-1.5 font-semibold w-20"
                          title="FinBERT tone — max |score| across the ticker's overnight clusters"
                        >
                          TONE
                        </th>
                        <th
                          className="px-2 py-1.5 font-semibold w-14"
                          title="news-buzz z-score vs the ticker's own history"
                        >
                          BUZZ
                        </th>
                        <th className="px-2 py-1.5 font-semibold">FLAGS</th>
                        <th
                          className="px-2 py-1.5 font-semibold w-16"
                          title="net directional lean across the ticker's overnight clusters"
                        >
                          LEAN
                        </th>
                        <th
                          className="px-2 py-1.5 font-semibold w-12"
                          title="age of the top catalyst at panel compute time"
                        >
                          AGE
                        </th>
                        <th
                          className="px-2 py-1.5 font-semibold w-40"
                          title="extended-session follow-through: premarket → regular → afterhours move vs prior close ('--' where a name had no extended print)"
                        >
                          PM→REG→AH
                        </th>
                        {pm.graded && (
                          <th
                            className="px-2 py-1.5 font-semibold w-16"
                            title="session open→close return (graded after the close)"
                          >
                            O→C
                          </th>
                        )}
                      </tr>
                    </thead>
                    <tbody>
                      {pm.rows.map((r, i) => {
                        const lc = leanChip(r.lean);
                        const oc = pm.outcomes?.[r.ticker];
                        const ex = extByTicker[r.ticker];
                        return (
                          <tr
                            key={r.ticker}
                            className="border-b border-tape-border-soft hover:bg-tape-panel-2"
                          >
                            <td className="px-4 py-1.5 text-tape-faint tabular-nums">{i + 1}</td>
                            <td className="px-2 py-1.5">
                              <TickerButton
                                ticker={r.ticker}
                                className="text-tape-text font-semibold hover:text-tape-accent"
                              />
                            </td>
                            <td className="px-2 py-1.5">
                              <div
                                className={`truncate max-w-[380px] leading-snug ${
                                  r.title ? "text-tape-sub" : "text-tape-dim"
                                }`}
                                title={r.title ? cleanTitle(r.title) : undefined}
                              >
                                {r.title ? cleanTitle(r.title) : "—"}
                              </div>
                            </td>
                            <td className="px-2 py-1.5 whitespace-nowrap">
                              {r.catalyst_type ? (
                                <span
                                  className={`px-1.5 py-0.5 rounded border border-tape-border-soft text-[10px] uppercase tracking-[0.06em] ${catColor(
                                    r.catalyst_type,
                                  )}`}
                                >
                                  {r.catalyst_type}
                                </span>
                              ) : (
                                <span className="text-tape-dim">—</span>
                              )}
                              {r.event_stage && (
                                <span className="ml-1 px-1.5 py-0.5 rounded border border-tape-border-soft text-[10px] uppercase tracking-[0.06em] text-tape-muted">
                                  {r.event_stage}
                                </span>
                              )}
                            </td>
                            <td className="px-2 py-1.5 text-tape-dim tabular-nums">
                              {r.materiality == null ? "—" : r.materiality.toFixed(2)}
                            </td>
                            <td className={`px-2 py-1.5 tabular-nums ${toneColor(r.finbert_score)}`}>
                              {toneTag(r.finbert_score)}
                            </td>
                            <td className="px-2 py-1.5 text-tape-sub tabular-nums">
                              {r.buzz_z == null
                                ? "—"
                                : `${r.buzz_z >= 0 ? "+" : ""}${r.buzz_z.toFixed(1)}`}
                            </td>
                            <td className="px-2 py-1.5">
                              <span className="flex flex-wrap gap-1">
                                {r.high_alert && (
                                  <span className="px-1.5 py-0.5 rounded border border-tape-border-soft text-[10px] text-tape-warn">
                                    HIGH ALERT
                                  </span>
                                )}
                                {r.earnings_today && (
                                  <span
                                    className="px-1.5 py-0.5 rounded border border-tape-border-soft text-[10px] text-tape-warn"
                                    title="earnings scheduled for this session (approximate — not a confirmed before-open time)"
                                  >
                                    ERN TODAY
                                  </span>
                                )}
                                {r.scheduled_only && (
                                  <span
                                    className="px-1.5 py-0.5 rounded border border-tape-border-soft text-[10px] text-tape-muted"
                                    title="no overnight news — on the panel only for today's scheduled earnings"
                                  >
                                    SCHED ONLY
                                  </span>
                                )}
                                {!r.high_alert && !r.earnings_today && !r.scheduled_only && (
                                  <span className="text-tape-dim">—</span>
                                )}
                              </span>
                            </td>
                            <td className="px-2 py-1.5">
                              {lc ? (
                                <span
                                  className={`px-1.5 py-0.5 rounded border border-tape-border-soft text-[10px] ${lc.cls}`}
                                >
                                  {lc.txt}
                                </span>
                              ) : (
                                <span className="text-tape-dim">—</span>
                              )}
                            </td>
                            <td className="px-2 py-1.5 text-tape-dim tabular-nums">
                              {pmAge(r.age_hours)}
                            </td>
                            <td className="px-2 py-1.5 tabular-nums text-[10px] whitespace-nowrap">
                              {ex ? (
                                <span title={streakPhrase(ex.premarket_streak) ?? undefined}>
                                  <span className={extPctCls(ex.pm_pct)}>{pctStr(ex.pm_pct)}</span>
                                  <span className="text-tape-dim"> → </span>
                                  <span className={extPctCls(ex.reg_pct)}>{pctStr(ex.reg_pct)}</span>
                                  <span className="text-tape-dim"> → </span>
                                  <span className={extPctCls(ex.ah_pct)}>{pctStr(ex.ah_pct)}</span>
                                </span>
                              ) : (
                                <span className="text-tape-faint">--</span>
                              )}
                            </td>
                            {pm.graded && (
                              <td
                                className={`px-2 py-1.5 tabular-nums ${
                                  oc == null
                                    ? "text-tape-faint"
                                    : oc.oc_return >= 0
                                    ? "text-tape-bull"
                                    : "text-tape-bear"
                                }`}
                              >
                                {oc == null
                                  ? "—"
                                  : `${oc.oc_return >= 0 ? "+" : ""}${(oc.oc_return * 100).toFixed(2)}%`}
                              </td>
                            )}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            ))}
        </section>
      )}

      {loading ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted">
          loading catalysts…
        </div>
      ) : !data.reachable ? (
        <div className="flex-1 flex items-center justify-center tape-mono text-[11px] text-tape-muted px-8 text-center">
          Prediction API not reachable on :8001 — start it with scripts/serve_api.py.
        </div>
      ) : (
        <div className="flex-1 flex overflow-hidden">
          {/* SCHEDULED (earnings calendar) */}
          <section className="flex-1 min-w-0 overflow-y-auto border-r border-tape-border-soft">
            <div className="sticky top-0 px-4 py-2 tape-mono text-[10px] text-tape-muted tracking-[0.12em] border-b border-tape-border bg-tape-panel-2 z-10">
              SCHEDULED · earnings calendar
            </div>
            {data.scheduled.length === 0 ? (
              <div className="px-4 py-3 tape-mono text-[10.5px] text-tape-faint">
                no scheduled events — run scripts/snapshot_events.py
              </div>
            ) : (
              <table className="w-full border-collapse tape-mono text-[11px]">
                <tbody>
                  {data.scheduled.map((e, i) => (
                    <tr
                      key={`${e.ticker}-${e.event_date}-${i}`}
                      className="border-b border-tape-border-soft hover:bg-tape-panel-2"
                    >
                      <td className="px-4 py-2 w-20">
                        <TickerButton
                          ticker={e.ticker}
                          className="text-tape-text font-semibold hover:text-tape-accent"
                        />
                      </td>
                      <td className="px-2 py-2 text-tape-faint">{e.catalyst_type}</td>
                      <td className="px-2 py-2 text-tape-sub w-28">{e.event_date}</td>
                      <td className={`px-2 py-2 w-20 ${daysColor(e.days_until)}`}>
                        {e.days_until === null ? "—" : `in ${e.days_until}d`}
                      </td>
                      <td className="px-3 py-2 text-tape-dim w-16">{e.source}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {/* FIRED (recent scored catalysts) — materiality-ranked over the chosen
              window (window-scaled half-life surfaces week-old majors), scrollable
              inside this flex-1 section so it never squeezes SCHEDULED. */}
          <section className="flex-1 min-w-0 overflow-y-auto">
            <div className="sticky top-0 px-4 py-2 flex items-center gap-2.5 tape-mono text-[10px] text-tape-muted tracking-[0.12em] border-b border-tape-border bg-tape-panel-2 z-10">
              <span>FIRED · recent catalysts</span>
              <span className="text-tape-sub tracking-normal">{data.fired.length}</span>
              <div className="ml-auto flex items-center gap-1">
                {[
                  { d: 1, l: "24H" },
                  { d: 2, l: "48H" },
                  { d: 7, l: "1W" },
                ].map(({ d, l }) => (
                  <button
                    key={d}
                    type="button"
                    onClick={() => setFiredDays(d)}
                    className={`px-1.5 py-0.5 rounded border text-[9.5px] tracking-normal ${
                      firedDays === d
                        ? "border-tape-accent text-tape-accent"
                        : "border-tape-border-soft text-tape-faint hover:text-tape-sub"
                    }`}
                    title={`show fired catalysts from the last ${l}`}
                  >
                    {l}
                  </button>
                ))}
              </div>
            </div>
            {data.fired.length === 0 ? (
              <div className="px-4 py-3 tape-mono text-[10.5px] text-tape-faint">
                no fired catalysts scored yet
              </div>
            ) : (
              <table className="w-full border-collapse tape-mono text-[11px]">
                <tbody>
                  {data.fired.map((c) => (
                    <tr
                      key={c.cluster_id}
                      className="border-b border-tape-border-soft hover:bg-tape-panel-2 align-top"
                    >
                      <td className="px-4 py-2 w-24">
                        <div className="flex flex-wrap gap-1">
                          {c.tickers.slice(0, 3).map((t) => (
                            <TickerButton
                              key={t.ticker}
                              ticker={t.ticker}
                              className="text-tape-text font-semibold hover:text-tape-accent"
                            />
                          ))}
                          {c.tickers.length === 0 && <span className="text-tape-dim">—</span>}
                        </div>
                      </td>
                      <td className="px-2 py-2">
                        <div className="leading-snug">
                          {c.url ? (
                            <a
                              href={c.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-tape-sub hover:text-tape-text hover:underline decoration-tape-dim"
                            >
                              {c.title ? cleanTitle(c.title) : "(untitled)"}
                            </a>
                          ) : (
                            <span className="text-tape-sub">
                              {c.title ? cleanTitle(c.title) : "(untitled)"}
                            </span>
                          )}
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5 mt-1 text-tape-faint">
                          <span
                            className={`px-1.5 py-0.5 rounded border border-tape-border-soft text-[10px] uppercase tracking-[0.06em] ${catColor(
                              c.catalyst_type,
                            )}`}
                          >
                            {c.catalyst_type}
                          </span>
                          {c.event_stage && (
                            <span className="px-1.5 py-0.5 rounded border border-tape-border-soft text-[10px] uppercase tracking-[0.06em] text-tape-muted">
                              {c.event_stage}
                            </span>
                          )}
                          {/* TAPE-style tone badge (finbert primary); LM + label on hover */}
                          <span className={`tabular-nums ${toneColor(c.finbert_score)}`} title={sentTooltip(c)}>
                            {toneTag(c.finbert_score)}
                          </span>
                          <span className="text-tape-dim">mat {c.materiality.toFixed(2)}</span>
                          {c.high_alert && <span className="text-tape-warn">· HIGH ALERT</span>}
                          <span className="text-tape-dim">· pub {fmtET(c.published_at)}</span>
                          {c.called_at ? (
                            <span
                              className="text-tape-sub"
                              title="when the system scored/classified this cluster (first call, stable across re-scores)"
                            >
                              · called {fmtET(c.called_at)}
                              <span className="text-tape-faint">
                                {" "}
                                · {agoShort(c.called_at)}
                                {callLag(c.published_at, c.called_at) && (
                                  <> · {callLag(c.published_at, c.called_at)} after pub</>
                                )}
                              </span>
                            </span>
                          ) : (
                            <span className="text-tape-dim">
                              · call time n/a (pre-tracking) · {agoShort(c.published_at)} since pub
                            </span>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </div>
      )}

      <HealthStrip
        note={
          data.reachable
            ? "prediction API :8001 · scheduled = Finviz earnings · read-only"
            : "prediction API offline :8001"
        }
      />
    </>
  );
}
