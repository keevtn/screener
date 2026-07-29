/**
 * Shared vs-own-history insight semantics — used by the screener's INSIGHT
 * strip and the single-ticker INSIGHT panel so a rating/phrase reads the same
 * everywhere. Tones are semantic; each surface maps them to its own palette.
 */

export type Tone = "bull" | "bear" | "muted";

/** Tape-style tone COLOR from a sentiment score — the LIVE tape's palette at the
 * ±0.15 thresholds. Shared so every feed row (LIVE, catalysts, ticker) colorizes
 * tone identically. */
export function toneColor(score: number | null | undefined): string {
  if (score == null) return "text-tape-dim";
  if (score > 0.15) return "text-tape-bull";
  if (score < -0.15) return "text-tape-bear";
  return "text-tape-muted";
}

/** Tape-style tone BADGE: an arrow (▲ bull / ▼ bear / ◆ neutral) plus the signed
 * score, e.g. "▲ +0.42"; "—" when there's no score. Matches the LIVE tape exactly
 * so tone reads the same wherever a headline/catalyst is shown. */
export function toneTag(score: number | null | undefined): string {
  if (score == null) return "—";
  const arrow = score > 0.15 ? "▲" : score < -0.15 ? "▼" : "◆";
  return `${arrow} ${score >= 0 ? "+" : ""}${score.toFixed(2)}`;
}

/** Rating label from a sentiment score (same ±0.15 thresholds the tape uses). */
export function ratingOf(s: number): { txt: string; tone: Tone } {
  if (s > 0.15) return { txt: "BULLISH", tone: "bull" };
  if (s < -0.15) return { txt: "BEARISH", tone: "bear" };
  return { txt: "NEUTRAL", tone: "muted" };
}

/** Plain-language read of a sentiment z-score vs the name's own history. */
export function sentPhrase(z: number | null): string | null {
  if (z == null) return null;
  if (z <= -1.5) return "unusually negative for this name";
  if (z >= 1.5) return "unusually positive for this name";
  return "near its own norm";
}
