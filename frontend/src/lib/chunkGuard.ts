/**
 * Stale-chunk recovery (the "localhost crashed with a 400" failure).
 *
 * A long-running (or auto-restarted) `next dev` can evict/rotate hashed webpack
 * chunks while a tab still holds the old runtime; the tab's next dynamic
 * `import()` or route navigation then fetches a chunk URL the server no longer
 * has → 400/404 → ChunkLoadError. Without a boundary that uncaught rejection is
 * a full-screen crash. A plain reload always fixes it (the fresh HTML references
 * fresh chunks), so: detect the signature, reload ONCE (sessionStorage-guarded
 * so a genuinely broken build can't reload-loop).
 */

const KEY = "tape:chunk-reloaded-at";
const WINDOW_MS = 30_000; // one auto-reload per 30s, then surface the error

export function isChunkError(err: unknown): boolean {
  const msg =
    err instanceof Error
      ? `${err.name} ${err.message}`
      : typeof err === "string"
      ? err
      : String((err as { message?: string })?.message ?? "");
  return (
    /ChunkLoadError|Loading chunk .* failed|Failed to fetch dynamically imported module|Importing a module script failed/i.test(
      msg,
    )
  );
}

/** Reload once for a stale chunk; returns false if we already just reloaded. */
export function reloadOnceForStaleChunk(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const last = Number(window.sessionStorage.getItem(KEY) ?? 0);
    if (Date.now() - last < WINDOW_MS) return false; // avoid a reload loop
    window.sessionStorage.setItem(KEY, String(Date.now()));
  } catch {
    /* sessionStorage unavailable -> still better to reload than stay crashed */
  }
  window.location.reload();
  return true;
}
