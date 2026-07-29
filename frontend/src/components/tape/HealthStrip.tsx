/**
 * System-health strip — sticky at the bottom of every desktop screener surface
 * (design 1h/2a). Feed liveness dots + a note. Values are placeholder until a
 * /health-style screener endpoint exists.
 */

interface Feed {
  label: string;
  state: "ok" | "lag" | "down";
}

const FEEDS: Feed[] = [
  { label: "EDGAR 14:31", state: "ok" },
  { label: "NEWSWIRE 14:32", state: "ok" },
  { label: "BARS lag 14m", state: "lag" },
];

const DOT: Record<Feed["state"], string> = {
  ok: "bg-tape-bull",
  lag: "bg-tape-warn tape-pulse-fast",
  down: "bg-tape-bear tape-pulse-fast",
};

export default function HealthStrip({ note }: { note?: string }) {
  return (
    <div className="sticky bottom-0 z-20 flex items-center gap-[22px] h-[30px] px-[22px] border-t border-tape-border bg-tape-rail tape-mono text-[9.5px] font-medium text-tape-faint tracking-[0.06em]">
      {FEEDS.map((f) => (
        <span key={f.label} className="flex items-center gap-1.5">
          <span className={`w-[5px] h-[5px] rounded-full ${DOT[f.state]}`} aria-hidden />
          {f.label}
        </span>
      ))}
      <span className="ml-auto">{note ?? "strip is position:sticky — survives page scroll"}</span>
    </div>
  );
}
