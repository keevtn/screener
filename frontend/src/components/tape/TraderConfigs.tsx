"use client";

import { useEffect, useState } from "react";
import { fetchSimConfigs, type SimConfigItem } from "@/lib/trader";

/**
 * Read-only view of the standing paper-trader's configs and their exit policies —
 * the vol_stop A/B at a glance. Each row shows the entry hypothesis (direction /
 * horizon / filters) and which exit arm it runs (vol_stop vs horizon_hold), so
 * the split is visible on the CONFIG page. Purely informational; nothing here
 * enables/places anything.
 */

function paramSummary(p: Record<string, unknown>): string {
  const bits: string[] = [];
  if (p.direction) bits.push(String(p.direction));
  if (p.horizon_trading_days != null) bits.push(`h${p.horizon_trading_days}`);
  if (p.catalyst_types) bits.push((p.catalyst_types as string[]).join("/"));
  if (p.high_alert_only) bits.push("high-alert");
  if (p.min_materiality != null) bits.push(`mat≥${p.min_materiality}`);
  if (p.max_mcap_musd != null) bits.push(`≤$${p.max_mcap_musd}M`);
  if (p.after_hours_only) bits.push("after-hrs");
  return bits.join(" · ");
}

export default function TraderConfigs() {
  const [items, setItems] = useState<SimConfigItem[]>([]);
  const [reachable, setReachable] = useState(true);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const r = await fetchSimConfigs();
      if (cancelled) return;
      setItems(r.items);
      setReachable(r.reachable);
      setLoaded(true);
    }
    load();
    const t = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const enabled = items.filter((c) => c.enabled);
  const volStop = enabled.filter((c) => (c.exit_policy?.kind ?? "horizon_hold") === "vol_stop").length;

  if (loaded && !reachable) return null; // API down — the page's other sections say so

  return (
    <section className="border border-tape-border rounded bg-tape-panel">
      <div className="flex items-center gap-3 px-4 py-2 border-b border-tape-border-soft">
        <h2 className="tape-mono text-[11px] font-semibold tracking-[0.12em] text-tape-muted uppercase">
          Trader configs · exit-policy A/B
        </h2>
        <span className="tape-mono text-[10px] text-tape-faint ml-auto">
          {enabled.length} enabled · {volStop} vol_stop · {enabled.length - volStop} horizon
        </span>
      </div>
      {items.length === 0 ? (
        <div className="px-4 py-6 tape-mono text-[11px] text-tape-muted text-center">
          {loaded ? "No trader configs." : "loading…"}
        </div>
      ) : (
        <table className="w-full border-collapse tape-mono text-[11px]">
          <caption className="sr-only">Paper-trading configs and their exit policies</caption>
          <thead>
            <tr className="text-tape-muted text-left tracking-[0.1em] border-b border-tape-border-soft">
              <th scope="col" className="px-4 py-2 font-semibold">CONFIG</th>
              <th scope="col" className="px-2 py-2 font-semibold w-16">STATE</th>
              <th scope="col" className="px-2 py-2 font-semibold w-28">EXIT</th>
              <th scope="col" className="px-3 py-2 font-semibold">ENTRY</th>
            </tr>
          </thead>
          <tbody>
            {items.map((c) => {
              const kind = c.exit_policy?.kind ?? "horizon_hold";
              const isVol = kind === "vol_stop";
              return (
                <tr key={c.config_id} className="border-b border-tape-border-soft hover:bg-tape-panel-2">
                  <td className="px-4 py-2 text-tape-text font-semibold">{c.name}</td>
                  <td className={`px-2 py-2 ${c.enabled ? "text-tape-bull" : "text-tape-dim"}`}>
                    {c.enabled ? "on" : "off"}
                  </td>
                  <td className={`px-2 py-2 font-semibold ${isVol ? "text-tape-warn" : "text-tape-sub"}`}>
                    {isVol ? `vol_stop·${(c.params.exit_policy as { atr_mult?: number })?.atr_mult ?? 2}×ATR` : "horizon"}
                  </td>
                  <td className="px-3 py-2 text-tape-faint">{paramSummary(c.params)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
