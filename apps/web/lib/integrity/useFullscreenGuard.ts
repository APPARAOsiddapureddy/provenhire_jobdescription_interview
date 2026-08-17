"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReportViolation } from "./types";
import { isActive } from "./types";
import type { IntegrityRuleState } from "@proven-hire/shared";

/**
 * Tracks fullscreen state and reports an exit as a violation. Browsers only
 * allow entering fullscreen from a user gesture, so this can't force entry —
 * it exposes `needsFullscreen` + `requestFullscreen` for the caller to render
 * a click-to-continue prompt (same pattern as AudioUnlockOverlay's autoplay
 * unlock), rather than a hook that silently does nothing on first mount.
 */
export function useFullscreenGuard(
  state: IntegrityRuleState | undefined,
  onViolation: ReportViolation,
) {
  const enabled = isActive(state);
  const [needsFullscreen, setNeedsFullscreen] = useState(false);
  // Tracks the PREVIOUS in-fullscreen value so the very first "not in
  // fullscreen yet" (before the candidate has ever clicked to enter) isn't
  // itself reported as an "exit".
  const wasFullscreenRef = useRef(false);

  useEffect(() => {
    if (!enabled) {
      setNeedsFullscreen(false);
      wasFullscreenRef.current = false;
      return;
    }

    const inFullscreen = Boolean(document.fullscreenElement);
    setNeedsFullscreen(!inFullscreen);
    wasFullscreenRef.current = inFullscreen;

    function onChange() {
      const nowFullscreen = Boolean(document.fullscreenElement);
      setNeedsFullscreen(!nowFullscreen);
      if (!nowFullscreen && wasFullscreenRef.current) {
        onViolation("fullscreen_required", "Fullscreen mode was exited.");
      }
      wasFullscreenRef.current = nowFullscreen;
    }
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [enabled, onViolation]);

  const requestFullscreen = useCallback(() => {
    document.documentElement.requestFullscreen?.().catch(() => {
      // Some browsers (Safari iOS, an already-denied permission) reject this
      // silently; the candidate just stays in the "needs fullscreen" prompt.
    });
  }, []);

  return { needsFullscreen: enabled && needsFullscreen, requestFullscreen };
}
