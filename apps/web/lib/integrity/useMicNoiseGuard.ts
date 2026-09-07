"use client";

import { useEffect, useRef } from "react";
import type { ReportViolation } from "./types";
import { isActive } from "./types";
import type { IntegrityRuleState } from "@proven-hire/shared";

/**
 * Naive "background voice / unauthorized audio" heuristic: an AnalyserNode
 * RMS reading (same technique as <DeviceCheck>'s level meter) sustained
 * above a loudness threshold. This is amplitude-only — it flags sustained
 * loud background noise, not actual multi-speaker diarization, which is a
 * materially harder problem out of scope for this pass. Takes the mic track
 * as a parameter (owned by the room/session) rather than capturing its own —
 * no second `getUserMedia` call, and no coupling to any particular transport.
 *
 * ``suspended`` pauses detection only while the AI itself is speaking, so
 * its own TTS bleeding back through the speakers can't self-report. It
 * deliberately does NOT pause during the candidate's turn: any audible
 * sound in the room makes Deepgram transcribe, which flips the floor to
 * USER_SPEAKING — so gating on "is it the candidate's turn" disabled the
 * detector at exactly the moment audio was present, and it could never
 * fire. Discrimination comes from WINDOW_FRAMES and LOUD_RATIO_REQUIRED
 * below instead.
 */
// Normalized (byte time-domain, centered-at-128) RMS. Calibrated against
// real measurements rather than guessed: a phone playing audio next to the
// mic reads peaks of ~0.03-0.12 on this scale, so the values this shipped
// with (0.35, then 0.12) sat at or above the PEAK of real background audio
// and could never trip.
const RMS_THRESHOLD = 0.035;
// Detection is a DUTY CYCLE over a rolling window, not a run of unbroken
// loud frames. Audio is inherently dynamic — it dips between syllables and
// beats — so a consecutive-frame counter reset constantly and sat at 0
// even while a phone was audibly playing (measured). Asking "what fraction
// of the last ~3s was above threshold" is what actually separates
// continuous background audio from incidental one-off noises (a cough, a
// door), while still tolerating audio's natural dips.
const WINDOW_FRAMES = 180; // ~3s at rAF rate
const LOUD_RATIO_REQUIRED = 0.4;
const COOLDOWN_MS = 8000; // don't re-report while still loud
// Observed levels are logged periodically so the threshold above can stay
// calibrated against real rooms/hardware instead of being guessed at.
const DIAGNOSTIC_LOG_MS = 3000;

export function useMicNoiseGuard(
  state: IntegrityRuleState | undefined,
  onViolation: ReportViolation,
  track: MediaStreamTrack | null,
  suspended: boolean,
) {
  const enabled = isActive(state);
  const lastReportRef = useRef(0);
  const suspendedRef = useRef(suspended);
  suspendedRef.current = suspended;

  useEffect(() => {
    const mediaStreamTrack = track;
    if (!enabled || !mediaStreamTrack) return;

    type WindowWithWebkit = Window & {
      webkitAudioContext?: typeof AudioContext;
    };
    const Ctor =
      window.AudioContext ?? (window as WindowWithWebkit).webkitAudioContext;
    if (!Ctor) return;

    const ctx = new Ctor();
    const stream = new MediaStream([mediaStreamTrack]);
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const data = new Uint8Array(analyser.frequencyBinCount);

    let raf: number;
    // Ring buffer of the last WINDOW_FRAMES "was this frame loud?" flags,
    // with a running count so the duty cycle is O(1) per frame.
    const loudWindow = new Uint8Array(WINDOW_FRAMES);
    let windowIdx = 0;
    let loudInWindow = 0;
    let windowFilled = false;

    function resetWindow() {
      loudWindow.fill(0);
      windowIdx = 0;
      loudInWindow = 0;
      windowFilled = false;
    }

    let peakSinceLog = 0;
    let rmsSumSinceLog = 0;
    let framesSinceLog = 0;
    let lastLogAt = Date.now();

    console.info("[micNoiseGuard] active — monitoring track", {
      label: mediaStreamTrack.label,
      threshold: RMS_THRESHOLD,
      windowFrames: WINDOW_FRAMES,
      loudRatioRequired: LOUD_RATIO_REQUIRED,
    });

    function tick() {
      if (suspendedRef.current) {
        resetWindow();
        raf = requestAnimationFrame(tick);
        return;
      }
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = ((data[i] ?? 128) - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / data.length);
      if (rms > peakSinceLog) peakSinceLog = rms;
      rmsSumSinceLog += rms;
      framesSinceLog += 1;

      const isLoud = rms > RMS_THRESHOLD ? 1 : 0;
      loudInWindow -= loudWindow[windowIdx]!;
      loudWindow[windowIdx] = isLoud;
      loudInWindow += isLoud;
      windowIdx += 1;
      if (windowIdx >= WINDOW_FRAMES) {
        windowIdx = 0;
        windowFilled = true;
      }
      const loudRatio = loudInWindow / WINDOW_FRAMES;

      const now = Date.now();
      if (
        windowFilled &&
        loudRatio >= LOUD_RATIO_REQUIRED &&
        now - lastReportRef.current > COOLDOWN_MS
      ) {
        lastReportRef.current = now;
        console.warn("[micNoiseGuard] VIOLATION reported", {
          loudRatio: loudRatio.toFixed(2),
        });
        onViolation(
          "microphone_monitoring",
          "loud_background_audio",
          "Sustained loud background audio detected.",
        );
        resetWindow();
      }

      if (now - lastLogAt >= DIAGNOSTIC_LOG_MS) {
        const mean = framesSinceLog > 0 ? rmsSumSinceLog / framesSinceLog : 0;
        console.info("[micNoiseGuard] levels", {
          peak: peakSinceLog.toFixed(4),
          mean: mean.toFixed(4),
          threshold: RMS_THRESHOLD,
          loudRatio: loudRatio.toFixed(2),
          ratioNeeded: LOUD_RATIO_REQUIRED,
        });
        peakSinceLog = 0;
        rmsSumSinceLog = 0;
        framesSinceLog = 0;
        lastLogAt = now;
      }
      raf = requestAnimationFrame(tick);
    }
    tick();

    return () => {
      cancelAnimationFrame(raf);
      ctx.close().catch(() => {});
    };
  }, [enabled, track, onViolation]);
}
