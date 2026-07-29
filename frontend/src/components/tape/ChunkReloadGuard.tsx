"use client";

import { useEffect } from "react";
import { isChunkError, reloadOnceForStaleChunk } from "@/lib/chunkGuard";

/**
 * Global listener for stale-chunk failures that happen OUTSIDE React render —
 * e.g. a dynamic `import()` inside a useEffect rejecting after the dev server
 * recycled its webpack cache. Catches the unhandled rejection / script error,
 * and if it matches the chunk signature reloads once (see lib/chunkGuard).
 * Mounted once in the root layout; renders nothing.
 */
export default function ChunkReloadGuard() {
  useEffect(() => {
    function onRejection(e: PromiseRejectionEvent) {
      if (isChunkError(e.reason) && reloadOnceForStaleChunk()) e.preventDefault();
    }
    function onError(e: ErrorEvent) {
      if (isChunkError(e.error ?? e.message) && reloadOnceForStaleChunk()) e.preventDefault();
    }
    window.addEventListener("unhandledrejection", onRejection);
    window.addEventListener("error", onError);
    return () => {
      window.removeEventListener("unhandledrejection", onRejection);
      window.removeEventListener("error", onError);
    };
  }, []);
  return null;
}
