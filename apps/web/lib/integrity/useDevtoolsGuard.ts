"use client";

import { useEffect } from "react";
import type { ReportViolation } from "./types";
import { isActive } from "./types";
import type { IntegrityRuleState } from "@proven-hire/shared";

// A docked DevTools panel reliably adds well over this many px to the
// outer/inner viewport delta. There is no real "devtools opened" browser
// API — this is the standard best-effort heuristic (window-dimension
// delta), not a security boundary. It can false-negative (undocked/
// separate-window DevTools) and false-positive (some window managers /
// browser extensions resize chrome similarly). Polled rather than
// event-driven since no resize event fires for devtools specifically.
const OPEN_THRESHOLD_PX = 160;
const POLL_MS = 1000;

export function useDevtoolsGuard(
  state: IntegrityRuleState | undefined,
  onViolation: ReportViolation,
) {
  const enabled = isActive(state);

  useEffect(() => {
    if (!enabled) return;
    let wasOpen = false;
    const interval = setInterval(() => {
      const widthDelta = window.outerWidth - window.innerWidth;
      const heightDelta = window.outerHeight - window.innerHeight;
      const isOpen =
        widthDelta > OPEN_THRESHOLD_PX || heightDelta > OPEN_THRESHOLD_PX;
      if (isOpen && !wasOpen) {
        onViolation(
          "devtools_detection",
          "devtools_detected",
          "Developer tools appear to be open.",
        );
      }
      wasOpen = isOpen;
    }, POLL_MS);
    return () => clearInterval(interval);
  }, [enabled, onViolation]);
}
