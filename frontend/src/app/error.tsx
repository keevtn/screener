"use client";

import { useEffect } from "react";
import { isChunkError, reloadOnceForStaleChunk } from "@/lib/chunkGuard";

/**
 * Route-segment error boundary: one failing surface degrades to a TAPE-styled
 * panel with RETRY / RELOAD instead of taking the whole page down (previously
 * any render/effect error became the dev overlay or a white screen). Stale-chunk
 * errors (dev-server recycle) auto-reload once — a reload always fixes those.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    if (isChunkError(error)) reloadOnceForStaleChunk();
  }, [error]);

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-3 tape-mono text-[11px] text-tape-muted px-8 text-center py-16">
      <div className="text-tape-bear font-semibold tracking-[0.1em]">PANEL ERROR</div>
      <div className="max-w-xl text-tape-faint break-words">
        {error.message || "something went wrong rendering this surface"}
      </div>
      <div className="flex gap-2 mt-1">
        <button
          onClick={reset}
          className="px-3 py-1.5 rounded border border-tape-accent text-tape-accent font-semibold tracking-[0.1em] hover:bg-tape-accent hover:text-tape-bg transition-colors"
        >
          RETRY
        </button>
        <button
          onClick={() => window.location.reload()}
          className="px-3 py-1.5 rounded border border-tape-border text-tape-sub hover:border-tape-accent transition-colors"
        >
          RELOAD PAGE
        </button>
      </div>
      <div className="text-tape-dim text-[10px]">
        other surfaces are unaffected — this boundary contains the failure
      </div>
    </div>
  );
}
