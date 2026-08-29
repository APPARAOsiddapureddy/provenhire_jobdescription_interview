import { z } from "zod";

/**
 * Tri-state enforcement level for one integrity/proctoring rule.
 *
 * OFF = disabled · MONITOR = log + candidate warnings, no auto-end from
 * strikes · STRICT = full enforcement (blocks the action and/or counts
 * toward the 3-strike auto-end, per rule — see IntegritySettingsSchema).
 */
export const IntegrityRuleStateSchema = z.enum(["off", "monitor", "strict"]);
export type IntegrityRuleState = z.infer<typeof IntegrityRuleStateSchema>;

/**
 * Global, DB-backed proctoring configuration — one singleton row, editable
 * from `/settings/integrity` and read by the live interview room at session
 * start. Every field independently OFF/MONITOR/STRICT; there is no
 * per-session override in v1.
 */
export const IntegritySettingsSchema = z.object({
  // Master toggle: when OFF, none of the client-side monitor hooks run at
  // all regardless of the individual rule values below.
  ai_behavior_analysis: IntegrityRuleStateSchema,
  camera_required: IntegrityRuleStateSchema,
  copy_paste_detection: IntegrityRuleStateSchema,
  devtools_detection: IntegrityRuleStateSchema,
  fullscreen_required: IntegrityRuleStateSchema,
  microphone_monitoring: IntegrityRuleStateSchema,
  // Multi-face / phone / no-face / low-light, one combined toggle.
  camera_ai_detection: IntegrityRuleStateSchema,
  // Auto-end after 3 repeated alerts for the SAME rule class. Only takes
  // effect when this field itself is "strict" — copy-paste never counts
  // toward strikes regardless of this setting.
  three_strike_auto_end: IntegrityRuleStateSchema,
  screen_recording_enabled: IntegrityRuleStateSchema,
  tab_switching_detection: IntegrityRuleStateSchema,
  // Pydantic's `str | None = None` serializes an unset value as JSON `null`
  // (not an absent key), so this must accept null too — plain `.optional()`
  // only allows `undefined` and would silently fail every read where the
  // agent hasn't set a timestamp, which is the common case for the
  // in-memory/offline repository.
  updated_at: z.string().nullish(),
});
export type IntegritySettings = z.infer<typeof IntegritySettingsSchema>;

/** All rules OFF — the safe default when nothing is configured yet. */
export const DEFAULT_INTEGRITY_SETTINGS: IntegritySettings = {
  ai_behavior_analysis: "strict",
  camera_required: "strict",
  copy_paste_detection: "off",
  devtools_detection: "off",
  fullscreen_required: "off",
  microphone_monitoring: "off",
  camera_ai_detection: "strict",
  three_strike_auto_end: "strict",
  screen_recording_enabled: "off",
  tab_switching_detection: "off",
};

/** Named presets offered on the settings page (client-side fill only). */
export const INTEGRITY_PRESETS: Record<
  "computer_lab" | "remote_exam_strict",
  IntegritySettings
> = {
  computer_lab: {
    ai_behavior_analysis: "monitor",
    camera_required: "off",
    copy_paste_detection: "monitor",
    devtools_detection: "off",
    fullscreen_required: "off",
    microphone_monitoring: "off",
    camera_ai_detection: "off",
    three_strike_auto_end: "off",
    screen_recording_enabled: "off",
    tab_switching_detection: "monitor",
  },
  remote_exam_strict: {
    ai_behavior_analysis: "strict",
    camera_required: "strict",
    copy_paste_detection: "strict",
    devtools_detection: "strict",
    fullscreen_required: "strict",
    microphone_monitoring: "strict",
    camera_ai_detection: "strict",
    three_strike_auto_end: "strict",
    screen_recording_enabled: "strict",
    tab_switching_detection: "strict",
  },
};

/**
 * Rule classes that count toward the 3-strike auto-end (spec: "no face,
 * extra faces, phone, tab leave, fullscreen exit, loud / multi-voice
 * audio"). Copy-paste is deliberately excluded — it blocks the action
 * silently in STRICT mode but never accumulates a strike.
 */
export const STRIKE_ELIGIBLE_RULES = [
  "camera_ai_detection",
  "tab_switching_detection",
  "fullscreen_required",
  "microphone_monitoring",
] as const;
export type StrikeEligibleRule = (typeof STRIKE_ELIGIBLE_RULES)[number];
