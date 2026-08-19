"use client";

/**
 * Real RMS mic-energy (0..1) from a shared mic track — same AnalyserNode
 * technique as `lib/integrity/useMicNoiseGuard.ts`, reused here purely for
 * the AI orb's visual amplitude while the candidate is expected to be
 * speaking. Takes the track as a parameter (owned by the room/session, e.g.
 * InterviewSession.micTrack) rather than capturing its own — no second
 * `getUserMedia` call, and no coupling to any particular transport.
 */

import * as React from "react";

export function useMicEnergy(active: boolean, track: MediaStreamTrack | null): number {
  const [energy, setEnergy] = React.useState(0);

  React.useEffect(() => {
    const mediaStreamTrack = track;
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
  }, [active, track]);

  return energy;
}
