/**
 * Verdict derivation — deliberately NOT in components/design-system.tsx
 * (which is "use client"): this is a plain function with no React/browser
 * dependency, and both server components (dashboard, report) and client
 * components need to call it directly. A function exported from a "use
 * client" module can only be rendered as a component from a Server
 * Component, never called directly — keeping this here avoids that RSC
 * boundary violation.
 */

export type PHVerdict =
  "STRONG_HIRE" | "HIRE" | "MAYBE" | "NO_HIRE" | "INSUFFICIENT_DATA";

/** Simple, explicit score → verdict mapping used where the backend doesn't
 * (yet) produce a hiring verdict directly — ScoreCard only has `overall_score`
 * + `coverage_pct`. Kept as a named export so every screen derives it the
 * same way instead of re-inventing thresholds. */
export function scoreToVerdict(
  overallScore: number,
  coveragePct = 1,
): PHVerdict {
  if (coveragePct < 0.34) return "INSUFFICIENT_DATA";
  if (overallScore >= 0.85) return "STRONG_HIRE";
  if (overallScore >= 0.65) return "HIRE";
  if (overallScore >= 0.4) return "MAYBE";
  return "NO_HIRE";
}
