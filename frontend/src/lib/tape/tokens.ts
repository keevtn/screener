/**
 * TAPE_ palette as JS constants, mirroring the tailwind `tape.*` colors and the
 * `.tape` CSS scope. Used by the few components that build exact dense grids
 * with inline styles (grid-template fractions Tailwind can't express cleanly).
 */
export const C = {
  bg: "#07090D",
  panel: "#0B0D12",
  panel2: "#0D1017",
  rail: "#0A0D13",
  border: "#1C2230",
  borderSoft: "#151A24",
  line: "#10141C",
  text: "#E6EAF2",
  sub: "#B9C0CF",
  muted: "#8B94A7",
  faint: "#5A6478",
  dim: "#3E4656",
  accent: "#4FD1C5",
  accentHi: "#8AE8DF",
  bull: "#34D399",
  bear: "#FB7185",
  warn: "#F0B44A",
} as const;

export const MONO = "'IBM Plex Mono', ui-monospace, monospace";
export const SANS = "'IBM Plex Sans', system-ui, sans-serif";

/** Color a signed number bull/bear/muted for the terminal grid. */
export function signColor(n: number, zeroIsMuted = true): string {
  if (n > 0) return C.bull;
  if (n < 0) return C.bear;
  return zeroIsMuted ? C.muted : C.text;
}
