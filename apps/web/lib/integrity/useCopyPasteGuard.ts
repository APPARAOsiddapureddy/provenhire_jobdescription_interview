"use client";

import { useEffect } from "react";
import type { ReportViolation } from "./types";
import type { IntegrityRuleState } from "@proven-hire/shared";

/**
 * Blocks copy/paste/right-click ONLY in STRICT (MONITOR just observes,
 * matching "MONITOR = log ... no auto-end", never blocking the candidate).
 * Per spec, copy-paste "does not use strike toasts" — it never accumulates
 * a strike and the violation callback is used for a lightweight blocked
 * notice, not the same toast the strike-eligible rules show.
 */
export function useCopyPasteGuard(
  state: IntegrityRuleState | undefined,
  onBlocked: ReportViolation,
) {
  useEffect(() => {
    if (!state || state === "off") return;
    const block = state === "strict";

    function handle(e: Event) {
      if (block) {
        e.preventDefault();
        onBlocked(
          "copy_paste_detection",
          "Copy/paste and the right-click menu are disabled during this assessment.",
        );
      } else {
        onBlocked("copy_paste_detection", "Copy/paste activity detected.");
      }
    }

    document.addEventListener("copy", handle);
    document.addEventListener("paste", handle);
    document.addEventListener("contextmenu", handle);
    return () => {
      document.removeEventListener("copy", handle);
      document.removeEventListener("paste", handle);
      document.removeEventListener("contextmenu", handle);
    };
  }, [state, onBlocked]);
}
