/**
 * Heuristic AI-echo detector for barge-in gating: with no headphones, the
 * candidate's own mic can pick up the AI's TTS output (speaker bleed), and
 * Deepgram will transcribe it as if the candidate spoke it. `msSinceAiSpoke`
 * covers both "AI is actively speaking right now" (0) and "AI just stopped,
 * but room echo/audio buffering can still trigger a stray fragment" (the
 * 1000ms cooldown after playback ends).
 */

const ECHO_COOLDOWN_MS = 1000;
const WORD_OVERLAP_THRESHOLD = 0.6;

function normalize(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function isLikelyAiEcho(
  candidateText: string,
  lastAiText: string,
  msSinceAiSpoke: number,
): boolean {
  if (!lastAiText || msSinceAiSpoke > ECHO_COOLDOWN_MS) return false;

  const a = normalize(candidateText);
  const b = normalize(lastAiText);
  if (!a || !b) return false;

  if (b.includes(a) || a.includes(b)) return true;

  const aWords = new Set(a.split(" "));
  const bWords = new Set(b.split(" "));
  const smaller = Math.min(aWords.size, bWords.size);
  if (smaller === 0) return false;

  let overlap = 0;
  for (const w of aWords) if (bWords.has(w)) overlap++;
  return overlap / smaller >= WORD_OVERLAP_THRESHOLD;
}
