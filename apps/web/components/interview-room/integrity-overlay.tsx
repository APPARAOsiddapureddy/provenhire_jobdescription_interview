"use client";

/**
 * Proctoring UI for <InterviewRoomLive> — the room that was previously
 * missing ALL integrity wiring (see the Integrity Controls rework plan).
 * Three pieces, each matching an existing visual pattern in this codebase
 * rather than inventing a new one:
 *
 *  - <IntegrityBanner>: adapts the mic-failure banner already in
 *    interview-room-client.tsx (role="alert", fixed top strip, PHButton).
 *  - <FullscreenExitCountdownModal> / <ProctoringBanModal>: built on
 *    <ModalScaffold> (exported from interview-room-floor.tsx), matching
 *    that file's own internal (unexported) CountdownModal.
 */

import * as React from "react";
import { ModalScaffold } from "./interview-room-floor";
import { PHButton } from "@/components/design-system";
import type { IntegrityBanner as IntegrityBannerData } from "@/lib/integrity";

export function IntegrityBanner({ banner }: { banner: IntegrityBannerData }) {
  const strict = banner.level === "strict";
  return (
    <div
      role="alert"
      className="fixed inset-x-0 top-0 z-50 flex items-center justify-center gap-4 px-6 py-3 text-center text-[13px] text-white shadow-lg"
      style={{ background: strict ? "#3a1010" : "#1a2a3a" }}
    >
      <span className="font-semibold">
        {strict ? "Integrity warning" : "Notice"}:
      </span>
      <span>{banner.message}</span>
    </div>
  );
}

/**
 * Click-to-continue gate for fullscreen — browsers only allow entering
 * fullscreen from a real user gesture, so this can't auto-resolve. A timed
 * auto-end countdown for a MID-session exit (vs. this same prompt showing
 * before the candidate has entered fullscreen even once) is a later pass —
 * it needs the hook to distinguish those two cases first (see the rework
 * plan's Phase 6); showing an auto-end timer here today would risk ending
 * brand-new sessions before the candidate has had a chance to react.
 */
export function FullscreenRequiredModal({
  onContinue,
}: {
  onContinue: () => void;
}) {
  return (
    <ModalScaffold titleId="ph-fullscreen-required-title">
      <p
        id="ph-fullscreen-required-title"
        className="text-[19px] font-semibold text-white"
      >
        Fullscreen required
      </p>
      <p className="text-[13px] leading-relaxed text-white/60">
        This assessment requires fullscreen mode. Click below to continue.
      </p>
      <PHButton variant="primary" onClick={onContinue}>
        Enter fullscreen
      </PHButton>
    </ModalScaffold>
  );
}

/** 10s delay after the weighted proctoring score crosses the ban threshold. */
export function ProctoringBanModal({ onTimeout }: { onTimeout: () => void }) {
  const [secondsLeft, setSecondsLeft] = React.useState(10);

  React.useEffect(() => {
    if (secondsLeft <= 0) {
      onTimeout();
      return;
    }
    const t = setTimeout(() => setSecondsLeft((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [secondsLeft, onTimeout]);

  return (
    <ModalScaffold titleId="ph-ban-title">
      <p id="ph-ban-title" className="text-[19px] font-semibold text-white">
        Assessment ending
      </p>
      <p className="text-[13px] leading-relaxed text-white/60">
        Repeated integrity violations were detected during this session. Ending
        in{" "}
        <span className="font-mono font-semibold text-[var(--ph-red)]">
          {secondsLeft}s
        </span>
        .
      </p>
    </ModalScaffold>
  );
}
