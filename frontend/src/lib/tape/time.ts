/**
 * Call-time formatting for catalyst surfaces. All wall-clock times render in
 * ET (the tape's timezone); ages are short relative strings. "Call time" is
 * ClusterScore.created_at — when the system first scored/classified a cluster
 * (stable across re-scores) — as opposed to the article's publish time.
 */

const ET = "America/New_York";

const timeFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const dateFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  month: "short",
  day: "numeric",
});

/** "14:32 ET", or "Jul 16 14:32 ET" when the ET calendar day isn't today's. */
export function fmtET(iso: string, now: number = Date.now()): string {
  const d = new Date(iso);
  if (isNaN(+d)) return "—";
  const t = `${timeFmt.format(d)} ET`;
  return dateFmt.format(d) === dateFmt.format(new Date(now)) ? t : `${dateFmt.format(d)} ${t}`;
}

/** "12m ago" / "3h ago" / "2d ago" (floors; honest about staleness). */
export function agoShort(iso: string, now: number = Date.now()): string {
  const ms = now - Date.parse(iso);
  if (isNaN(ms) || ms < 0) return "—";
  const m = Math.floor(ms / 60_000);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** Publish→call lag, shown only when it's meaningful (≥10 min): "+47m" / "+2.1h". */
export function callLag(publishedIso: string, calledIso: string): string | null {
  const ms = Date.parse(calledIso) - Date.parse(publishedIso);
  if (isNaN(ms) || ms < 10 * 60_000) return null;
  const m = Math.round(ms / 60_000);
  return m < 120 ? `+${m}m` : `+${(m / 60).toFixed(1)}h`;
}
