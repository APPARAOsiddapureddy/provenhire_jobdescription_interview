import type { IntegrityRuleState, ProctoringSeverity } from "@proven-hire/shared";

/**
 * The rule classes a live guard hook can report against. `screen_recording_enabled`
 * isn't here — it's consent + capture, not a detector with violations.
 */
export type GuardRuleClass =
  | "fullscreen_required"
  | "tab_switching_detection"
  | "devtools_detection"
  | "copy_paste_detection"
  | "microphone_monitoring"
  | "camera_ai_detection";

/**
 * A guard hook's report: "something happened for this rule." Severity/strike
 * policy (MONITOR vs STRICT) is resolved centrally by useIntegrityMonitor,
 * not by the individual hook — keeps the OFF/MONITOR/STRICT decision in one
 * place instead of duplicated per guard.
 *
 * `eventType` is the free-form string persisted to the generic proctoring
 * event log and looked up in packages/shared/data/proctoring-weights.json
 * for scoring (see useIntegrityMonitor) — most detectors emit a type with
 * weight 0 (logged only); a handful (fullscreen_exit, window_blur,
 * face_missing, multiple_faces) carry real weight toward the ban threshold.
 */
export type ReportViolation = (
  rule: GuardRuleClass,
  eventType: string,
  message: string,
  opts?: { photo?: string; severity?: ProctoringSeverity },
) => void;

/** True whenever a rule is anything other than fully disabled. */
export function isActive(state: IntegrityRuleState | undefined): boolean {
  return state !== undefined && state !== "off";
}
