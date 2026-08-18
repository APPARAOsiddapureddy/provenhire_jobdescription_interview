import { z } from "zod";
// Lives in apps/agent/data/, NOT packages/shared/data/: every Python
// deployment in this repo (Render's agent-api, the LiveKit Cloud worker)
// Docker-builds with apps/agent as its OWN build context, never seeing
// packages/shared/ at all — so the single physical source of truth has to
// live somewhere every deployment target can actually reach. This is that
// somewhere; .vercelignore carries a narrow exception so the web build can
// still see this one file despite excluding the rest of apps/agent.
import weightsData from "../../../apps/agent/data/proctoring-weights.json";

/**
 * Generic proctoring event log — the "add a new detector without a
 * migration" layer described in the Integrity Controls rework plan. `type`
 * is a free-form string (e.g. "face_missing", "window_blur"), NOT an enum:
 * a new detector just picks a new string and, optionally, a weight below.
 * Severity/enforcement LEVEL (OFF/MONITOR/STRICT) still comes from
 * `IntegritySettings` (see integrity.ts) — this schema is the event log
 * underneath it, not a replacement for it.
 */
export const ProctoringSeveritySchema = z.enum(["info", "warning", "critical"]);
export type ProctoringSeverity = z.infer<typeof ProctoringSeveritySchema>;

export const ProctoringEventCreateSchema = z.object({
  session_id: z.string(),
  type: z.string(),
  severity: ProctoringSeveritySchema,
  message: z.string(),
  // A `data:` URL or an R2 https URL — never raw bytes. Optional; only
  // attached for nonzero-weight (real) violations, not every diagnostic
  // event. No .max() here: size ceilings are enforced in the route handler,
  // not the shared contract (matches PrepRequest's cv_url convention).
  photo: z.string().nullish(),
});
export type ProctoringEventCreate = z.infer<typeof ProctoringEventCreateSchema>;

export const ProctoringEventSchema = ProctoringEventCreateSchema.extend({
  id: z.string(),
  created_at: z.string(),
});
export type ProctoringEvent = z.infer<typeof ProctoringEventSchema>;

export const ProctoringEventResponseSchema = z.object({
  event: ProctoringEventSchema,
  score: z.number(),
  ban_triggered: z.boolean(),
});
export type ProctoringEventResponse = z.infer<typeof ProctoringEventResponseSchema>;

/**
 * The single physical source of truth for weight/decay/threshold values,
 * read from ../data/proctoring-weights.json — the SAME file the Python
 * agent reads at runtime (apps/agent/.../core/proctoring/weights.py). This
 * is the fix for the reference implementation's own footgun: it kept this
 * table in two hand-synced places (TS client + a duplicated SQL CASE on
 * the server); here there is exactly one authored copy on disk.
 */
interface ProctoringWeightsFile {
  decay_per_step: number;
  decay_step_sec: number;
  strike_threshold_default: number;
  weights: Record<string, number>;
}
const weightsFile = weightsData as ProctoringWeightsFile;

export const PROCTORING_WEIGHTS: Record<string, number> = weightsFile.weights;
export const PROCTORING_DECAY_PER_STEP = weightsFile.decay_per_step;
export const PROCTORING_DECAY_STEP_SEC = weightsFile.decay_step_sec;
export const PROCTORING_STRIKE_THRESHOLD_DEFAULT = weightsFile.strike_threshold_default;

/** Any type not in the table is logged-only (weight 0) — never throws. */
export function weightFor(type: string): number {
  return PROCTORING_WEIGHTS[type] ?? 0;
}
