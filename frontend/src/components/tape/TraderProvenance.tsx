import type { Provenance } from "@/lib/trader";
import { isHttp } from "@/lib/trader";

/**
 * The provenance cell — the TRADER view's differentiator. Joins a fill/position
 * back to OUR DB: the sim config that opened it and the originating catalyst
 * headline ("the agent bought X — here's the headline that caused it"). Null
 * provenance (no matching sim_trade for the Alpaca order — manual or pre-account
 * trades) renders an honest em-dash, never a guess.
 */
export default function TraderProvenance({ prov }: { prov: Provenance | null }) {
  if (!prov || (!prov.config_name && !prov.headline && !prov.catalyst_type)) {
    return <span className="text-tape-faint">—</span>;
  }
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-1.5">
        {prov.config_name && (
          <span
            className="text-[9px] font-semibold uppercase tracking-[0.06em] text-tape-accent border border-[#1F3B38] bg-[rgba(79,209,197,0.10)] rounded px-1 py-px"
            title={`sim config: ${prov.config_name}`}
          >
            {prov.config_name}
          </span>
        )}
        {prov.exit_policy && prov.exit_policy !== "horizon_hold" && (
          <span
            className="text-[9px] font-semibold uppercase tracking-[0.06em] text-tape-warn border border-tape-border rounded px-1 py-px"
            title={`exit policy: ${prov.exit_policy}${prov.exit_reason ? ` · exited: ${prov.exit_reason}` : ""}`}
          >
            {prov.exit_policy}
          </span>
        )}
        {prov.catalyst_type && (
          <span
            className={`text-[9px] font-semibold uppercase tracking-[0.06em] rounded px-1 py-px border ${
              prov.high_alert
                ? "text-tape-warn border-tape-border bg-[rgba(240,180,74,0.10)]"
                : "text-tape-sub border-tape-border-soft"
            }`}
            title={prov.high_alert ? "high-alert catalyst" : "catalyst type"}
          >
            {prov.catalyst_type}
          </span>
        )}
        {prov.source_class && (
          <span className="text-[9px] uppercase tracking-[0.06em] text-tape-dim">
            {prov.source_class}
          </span>
        )}
      </div>
      {prov.headline &&
        (isHttp(prov.url) ? (
          <a
            href={prov.url}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className="text-tape-sub hover:text-tape-accent truncate max-w-[24rem] underline decoration-tape-border-soft underline-offset-2"
            title={prov.headline}
          >
            {prov.headline}
          </a>
        ) : (
          <span className="text-tape-sub truncate max-w-[24rem]" title={prov.headline}>
            {prov.headline}
          </span>
        ))}
      {prov.source && <span className="text-tape-dim text-[10px] truncate max-w-[16rem]">{prov.source}</span>}
    </div>
  );
}
