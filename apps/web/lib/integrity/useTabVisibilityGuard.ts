"use client";

import { useEffect } from "react";
import type { ReportViolation } from "./types";
import { isActive } from "./types";
import type { IntegrityRuleState } from "@proven-hire/shared";

/** Reports a violation whenever the tab is hidden (switched away / minimized). */
export function useTabVisibilityGuard(
  state: IntegrityRuleState | undefined,
  onViolation: ReportViolation,
) {
  const enabled = isActive(state);

  useEffect(() => {
    if (!enabled) return;
    function onVisibilityChange() {
      if (document.hidden) {
        onViolation(
          "tab_switching_detection",
          "Switched away from the interview tab.",
        );
      }
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () =>
      document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [enabled, onViolation]);
}
