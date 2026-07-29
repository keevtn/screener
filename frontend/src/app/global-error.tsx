"use client";

import { useEffect } from "react";
import { isChunkError, reloadOnceForStaleChunk } from "@/lib/chunkGuard";

/**
 * Last-resort boundary (errors in the root layout itself). Must render its own
 * <html>/<body>. Same policy as error.tsx: auto-reload once on stale chunks,
 * otherwise a minimal reload screen instead of a dead tab.
 */
export default function GlobalError({ error }: { error: Error & { digest?: string } }) {
  useEffect(() => {
    if (isChunkError(error)) reloadOnceForStaleChunk();
  }, [error]);

  return (
    <html>
      <body style={{ background: "#07090d", color: "#8b93a7", fontFamily: "monospace" }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12, paddingTop: "20vh", textAlign: "center" }}>
          <div style={{ color: "#fb7185", letterSpacing: "0.1em", fontWeight: 600 }}>
            TAPE_ CRASHED
          </div>
          <div style={{ maxWidth: 560, fontSize: 12 }}>{error.message || "unrecoverable error"}</div>
          <button
            onClick={() => window.location.reload()}
            style={{
              marginTop: 8,
              padding: "6px 14px",
              background: "transparent",
              border: "1px solid #4fd1c5",
              color: "#4fd1c5",
              letterSpacing: "0.1em",
              cursor: "pointer",
            }}
          >
            RELOAD
          </button>
        </div>
      </body>
    </html>
  );
}
