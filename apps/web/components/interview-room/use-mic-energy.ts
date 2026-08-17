"use client";

/**
 * Real RMS mic-energy (0..1) from the LOCAL participant's already-published
 * LiveKit mic track — same AnalyserNode technique as
 * `lib/integrity/useMicNoiseGuard.ts`, reused here purely for the AI orb's
 * visual amplitude while the candidate is expected to be speaking. No second
 * `getUserMedia` call; this taps the SAME track already flowing to the
 * agent, so the orb genuinely reacts to what's being said.
 */

import * as React from "react";
import { useLocalParticipant } from "@livekit/components-react";

export function useMicEnergy(active: boolean): number {
  const { microphoneTrack } = useLocalParticipant();
  const [energy, setEnergy] = React.useState(0);

  React.useEffect(() => {
    const mediaStreamTrack = microphoneTrack?.track?.mediaStreamTrack;
    if (!active || !mediaStreamTrack) {
      setEnergy(0);
      return;
    }

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

    function tick() {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = ((data[i] ?? 128) - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / data.length);
      // Typical conversational RMS sits well under 1.0 — scale up for a
      // visually legible pulse without changing what's actually measured.
      setEnergy(Math.max(0, Math.min(1, rms * 2.5)));
      raf = requestAnimationFrame(tick);
    }
    tick();

    return () => {
      cancelAnimationFrame(raf);
      ctx.close().catch(() => {});
    };
  }, [active, microphoneTrack]);

  return energy;
}
