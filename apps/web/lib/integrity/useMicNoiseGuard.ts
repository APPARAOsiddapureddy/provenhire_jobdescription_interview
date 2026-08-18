"use client";

import { useEffect, useRef } from "react";
import { useLocalParticipant } from "@livekit/components-react";
import type { ReportViolation } from "./types";
import { isActive } from "./types";
import type { IntegrityRuleState } from "@proven-hire/shared";

/**
 * Naive "background voice / unauthorized audio" heuristic: an AnalyserNode
 * RMS reading (same technique as <DeviceCheck>'s level meter) sustained
 * above a loudness threshold for several consecutive seconds. This is
 * amplitude-only — it flags sustained loud background noise, not actual
 * multi-speaker diarization, which is a materially harder problem out of
 * scope for this pass. Reuses the LOCAL PARTICIPANT'S already-published mic
 * track (LiveKit's own for the voice interview) rather than requesting the
 * microphone a second time via a fresh getUserMedia call.
 */
const RMS_THRESHOLD = 0.35;
const SUSTAINED_FRAMES = 45; // ~0.75s at rAF rate before it counts as a violation
const COOLDOWN_MS = 8000; // don't re-report while still loud

export function useMicNoiseGuard(
  state: IntegrityRuleState | undefined,
  onViolation: ReportViolation,
) {
  const enabled = isActive(state);
  const { microphoneTrack } = useLocalParticipant();
  const lastReportRef = useRef(0);

  useEffect(() => {
    const mediaStreamTrack = microphoneTrack?.track?.mediaStreamTrack;
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

    let loudFrames = 0;
    let raf: number;

    function tick() {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = ((data[i] ?? 128) - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / data.length);

      if (rms > RMS_THRESHOLD) {
        loudFrames += 1;
        const now = Date.now();
        if (
          loudFrames >= SUSTAINED_FRAMES &&
          now - lastReportRef.current > COOLDOWN_MS
        ) {
          lastReportRef.current = now;
          onViolation(
            "microphone_monitoring",
            "loud_background_audio",
            "Sustained loud background audio detected.",
          );
        }
      } else {
        loudFrames = 0;
      }
      raf = requestAnimationFrame(tick);
    }
    tick();

    return () => {
      cancelAnimationFrame(raf);
      ctx.close().catch(() => {});
    };
  }, [enabled, microphoneTrack, onViolation]);
}
