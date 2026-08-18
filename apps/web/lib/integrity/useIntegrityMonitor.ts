"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  DEFAULT_INTEGRITY_SETTINGS,
  IntegritySettingsSchema,
  STRIKE_ELIGIBLE_RULES,
  type IntegrityRuleState,
  type IntegritySettings,
  type ProctoringSeverity,
} from "@proven-hire/shared";
import { useFullscreenGuard } from "./useFullscreenGuard";
import { useTabVisibilityGuard } from "./useTabVisibilityGuard";
import { useDevtoolsGuard } from "./useDevtoolsGuard";
import { useCopyPasteGuard } from "./useCopyPasteGuard";
import { useMicNoiseGuard } from "./useMicNoiseGuard";
import { useCameraMonitor } from "./useCameraMonitor";
import type { GuardRuleClass } from "./types";

export interface IntegrityBanner {
  rule: GuardRuleClass;
  message: string;
  level: "monitor" | "strict";
}

const BANNER_MS = 4500;

/**
 * Central OFF/MONITOR/STRICT policy + per-rule-class strike tracking for the
 * live interview room. Individual guard hooks (fullscreen/tab/devtools/
 * copy-paste/mic-noise) just report "this happened for this rule" — this
 * hook is the one place that decides what MONITOR vs STRICT means (toast
 * only vs toast + strike) and when 3 strikes on the SAME rule class should
 * end the session, matching the settings page's documented semantics.
 *
 * Every violation ALSO fire-and-forgets a POST to the generic proctoring
 * event log (`/api/proctoring-events`, see the Integrity Controls rework
 * plan) and reads back the session's cumulative weighted-decay strike
 * score. Crossing the ban threshold sets `banned` — additive to, not a
 * replacement for, the existing per-rule 3-strike auto-end below; whichever
 * fires first ends the interview. `banned` itself does NOT call onAutoEnd
 * directly: the consumer renders a ban countdown modal (own timer) that
 * calls onAutoEnd when it completes, mirroring the fullscreen-exit modal.
 */
export function useIntegrityMonitor(sessionId: string, onAutoEnd: () => void) {
  const [settings, setSettings] = useState<IntegritySettings | null>(null);
  const [banner, setBanner] = useState<IntegrityBanner | null>(null);
  const [banned, setBanned] = useState(false);
  const strikesRef = useRef<Partial<Record<GuardRuleClass, number>>>({});
  const bannerTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/integrity-settings")
      .then((r) => r.json())
      .then((json) => {
        if (cancelled) return;
        const parsed = IntegritySettingsSchema.safeParse(json);
        setSettings(parsed.success ? parsed.data : DEFAULT_INTEGRITY_SETTINGS);
      })
      .catch(() => {
        if (!cancelled) setSettings(DEFAULT_INTEGRITY_SETTINGS);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(
    () => () => {
      if (bannerTimerRef.current) clearTimeout(bannerTimerRef.current);
    },
    [],
  );

  const reportViolation = useCallback(
    (
      rule: GuardRuleClass,
      eventType: string,
      message: string,
      opts?: { photo?: string; severity?: ProctoringSeverity },
    ) => {
      const level = settings?.[rule];
      if (!level || level === "off") return; // guard fired after settings changed underneath it

      if (
        level === "strict" &&
        (STRIKE_ELIGIBLE_RULES as readonly string[]).includes(rule)
      ) {
        const next = (strikesRef.current[rule] ?? 0) + 1;
        strikesRef.current[rule] = next;
        if (next >= 3 && settings?.three_strike_auto_end === "strict") {
          onAutoEnd();
          return;
        }
      }

      setBanner({ rule, message, level });
      if (bannerTimerRef.current) clearTimeout(bannerTimerRef.current);
      bannerTimerRef.current = setTimeout(() => setBanner(null), BANNER_MS);

      // Fire-and-forget: persist the event + get back the authoritative
      // weighted-decay score. A failed/slow POST never blocks the banner
      // above (already shown synchronously) — this only adds the ban path.
      fetch("/api/proctoring-events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          type: eventType,
          severity: opts?.severity ?? (level === "strict" ? "warning" : "info"),
          message,
          photo: opts?.photo ?? null,
        }),
      })
        .then((r) => r.json())
        .then((json: { ban_triggered?: boolean }) => {
          if (json.ban_triggered) setBanned(true);
        })
        .catch(() => {
          // Offline/agent-down: the interview already isn't blocked on this
          // (matches /api/integrity-settings' fail-open posture).
        });
    },
    [settings, onAutoEnd, sessionId],
  );

  // "AI Behavior Analysis" is the master switch: OFF means none of the
  // granular guards run at all, regardless of their own individual values
  // (see IntegrityForm's row for this rule). Everything else stays
  // independently controllable once the master switch is on.
  const masterOn = settings ? settings.ai_behavior_analysis !== "off" : false;
  const gated = (
    rule: keyof Omit<IntegritySettings, "updated_at">,
  ): IntegrityRuleState | undefined => (masterOn ? settings?.[rule] : "off");

  // Every guard hook is called unconditionally every render (rules of
  // hooks); each is individually a no-op internally while its own setting
  // is "off" or settings haven't loaded yet.
  const { needsFullscreen, requestFullscreen } = useFullscreenGuard(
    gated("fullscreen_required"),
    reportViolation,
  );
  useTabVisibilityGuard(gated("tab_switching_detection"), reportViolation);
  useDevtoolsGuard(gated("devtools_detection"), reportViolation);
  useCopyPasteGuard(gated("copy_paste_detection"), reportViolation);
  useMicNoiseGuard(gated("microphone_monitoring"), reportViolation);
  // camera_required is a capability gate (like the mic), not a violation
  // rule — it isn't gated by the master switch, matching how mic access
  // itself isn't either; camera_ai_detection (the actual detector) is.
  const { status: cameraStatus } = useCameraMonitor(
    settings?.camera_required,
    gated("camera_ai_detection"),
    reportViolation,
  );

  return {
    settings,
    banner,
    banned,
    needsFullscreen,
    requestFullscreen,
    cameraStatus,
    integrityActive: masterOn,
  };
}
