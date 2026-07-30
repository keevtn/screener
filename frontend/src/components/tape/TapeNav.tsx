import Link from "next/link";
import { useEffect, useState } from "react";
import { useNavBadges, type NavBadges } from "@/lib/tape/navBadges";

/**
 * TAPE_ top navigation. Sticky terminal chrome shared by every screener surface.
 * LIVE points back at the existing news-feed dashboard (`/`); the current screen
 * is underlined in teal. Not-yet-built surfaces are rendered disabled.
 *
 * "New since last looked" badges ride the LIVE / CATALYSTS / LEDGER items (there
 * is no separate ALERTS panel — CATALYSTS already covers alerting). Counts come
 * from useNavBadges (localStorage last-seen); the active surface is never badged.
 */

export type TapeSection =
  | "LIVE"
  | "SCREENER"
  | "UNIVERSE"
  | "CATALYSTS"
  | "RANK"
  | "LEDGER"
  | "TRADER"
  | "EVAL"
  | "CONFIG";

const NAV: { label: TapeSection; href: string | null }[] = [
  { label: "LIVE", href: "/" },
  { label: "SCREENER", href: "/screener" },
  { label: "UNIVERSE", href: "/universe" },
  { label: "CATALYSTS", href: "/catalysts" },
  { label: "RANK", href: "/rank" },
  { label: "LEDGER", href: "/ledger" },
  { label: "TRADER", href: "/trader" },
  { label: "EVAL", href: "/eval" },
  { label: "CONFIG", href: "/config" },
];

function cap(n: number): string {
  return n > 99 ? "99+" : String(n);
}

/** Small count chip for a nav item; `alert` tints it red (high-alert catalysts). */
function Badge({ n, alert, title }: { n: number; alert?: boolean; title?: string }) {
  if (n <= 0) return null;
  return (
    <span
      title={title}
      className={`ml-1 inline-flex items-center justify-center min-w-[15px] h-[15px] px-1 rounded-full tape-mono text-[8.5px] font-bold leading-none align-middle ${
        alert
          ? "bg-tape-bear text-tape-bg"
          : "bg-[rgba(79,209,197,0.16)] text-tape-accent border border-[#1F3B38]"
      }`}
    >
      {cap(n)}
    </span>
  );
}

/** The badge(s) for one nav label, or null. Suppressed on the active surface. */
function navBadge(label: TapeSection, badges: NavBadges, isActive: boolean): React.ReactNode {
  if (isActive) return null;
  if (label === "LIVE") {
    return <Badge n={badges.LIVE.count} title={`${badges.LIVE.count} new items since last viewed`} />;
  }
  if (label === "CATALYSTS") {
    const { count, high } = badges.CATALYSTS;
    return (
      <Badge
        n={count}
        alert={high > 0}
        title={`${count} new fired${high > 0 ? ` · ${high} high-alert` : ""} since last viewed`}
      />
    );
  }
  if (label === "LEDGER") {
    const { count, graded } = badges.LEDGER;
    return (
      <span className="inline-flex items-center align-middle">
        <Badge n={count} title={`${count} new prediction(s) since last viewed`} />
        {graded > 0 && (
          <span
            title={`${graded} newly graded since last viewed`}
            className="ml-1 inline-flex items-center justify-center min-w-[15px] h-[15px] px-1 rounded-full tape-mono text-[8.5px] font-bold leading-none bg-[rgba(52,211,153,0.16)] text-tape-bull border border-[#1C3A2C]"
          >
            ✓{cap(graded)}
          </span>
        )}
      </span>
    );
  }
  return null;
}

function fmt(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())} ET`;
}

/** Ticking wall clock. Empty on first paint so SSR and client markup match. */
function useClock(): string {
  const [t, setT] = useState("");
  useEffect(() => {
    setT(fmt(new Date()));
    const id = setInterval(() => setT(fmt(new Date())), 1000);
    return () => clearInterval(id);
  }, []);
  return t;
}

export default function TapeNav({
  active,
  clock,
}: {
  active: TapeSection;
  clock: string;
}) {
  const badges = useNavBadges();
  return (
    <div className="sticky top-0 z-30 flex items-center gap-6 h-12 px-[22px] border-b border-tape-border bg-tape-panel-2">
      <div className="tape-mono font-bold text-sm text-tape-text tracking-[0.08em]">
        TAPE<span className="text-tape-accent">_</span>
      </div>
      <nav className="flex gap-[18px] tape-mono text-[10.5px] font-semibold tracking-[0.12em]">
        {NAV.map(({ label, href }) => {
          const isActive = label === active;
          const cls = isActive
            ? "text-tape-text border-b-2 border-tape-accent pb-[15px] -mb-[17px]"
            : href
            ? "text-tape-faint hover:text-tape-sub transition-colors"
            : "text-tape-dim cursor-not-allowed";
          const badge = navBadge(label, badges, isActive);
          if (href && !isActive) {
            return (
              <Link key={label} href={href} className={cls}>
                {label}
                {badge}
              </Link>
            );
          }
          return (
            <span key={label} className={cls} aria-current={isActive ? "page" : undefined}>
              {label}
              {badge}
            </span>
          );
        })}
      </nav>
      <div className="ml-auto flex items-center gap-4">
        <span className="tape-mono text-[11px] font-medium text-tape-muted tabular-nums">
          {clock}
        </span>
        <span className="w-[7px] h-[7px] rounded-full bg-tape-bull tape-pulse" aria-hidden />
      </div>
    </div>
  );
}

export { useClock };
