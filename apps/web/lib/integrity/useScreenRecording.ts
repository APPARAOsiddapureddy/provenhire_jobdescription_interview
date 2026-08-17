"use client";

/**
 * <useScreenRecording> — "Screen Recording Enabled".
 *
 * getDisplayMedia() always requires a real user gesture and always shows the
 * browser's own picker/consent UI — there is no way around that, so this
 * hook exposes `needsConsent` for the caller to render a click-to-continue
 * gate (the same BlockingPrompt template as fullscreen/audio) rather than
 * trying to request automatically. Recording only ever starts after that
 * explicit click.
 *
 * Chunks are buffered in memory (MediaRecorder timeslice) and uploaded to R2
 * as ONE blob via `stopAndUpload()`, which the caller must invoke before
 * leaving the room (see live-room.tsx's endInterview) — gated on R2 being
 * configured server-side, since there's nowhere else large recordings could
 * reasonably go (unlike CVs, there's no small-file offline fallback here).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { IntegrityRuleState } from "@proven-hire/shared";
import { isActive } from "./types";

export type RecordingStatus =
  "idle" | "needs-consent" | "recording" | "uploading" | "done" | "error";

const CHUNK_MS = 1000;

export function useScreenRecording(
  state: IntegrityRuleState | undefined,
  sessionId: string,
  r2Configured: boolean,
) {
  const enabled = isActive(state) && r2Configured;
  const [status, setStatus] = useState<RecordingStatus>("idle");

  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  useEffect(() => {
    setStatus(enabled ? "needs-consent" : "idle");
  }, [enabled]);

  // Always clean up any live tracks on unmount, regardless of how the
  // session ended.
  useEffect(
    () => () => {
      streamRef.current?.getTracks().forEach((tr) => tr.stop());
    },
    [],
  );

  const stopAndUpload = useCallback(async () => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") return;

    setStatus("uploading");
    await new Promise<void>((resolve) => {
      recorder.onstop = () => resolve();
      recorder.stop();
    });
    streamRef.current?.getTracks().forEach((tr) => tr.stop());
    streamRef.current = null;

    const blob = new Blob(chunksRef.current, { type: "video/webm" });
    chunksRef.current = [];
    if (blob.size === 0) {
      setStatus("done");
      return;
    }

    try {
      const presignRes = await fetch("/api/recordings", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          filename: `${sessionId}.webm`,
          content_type: "video/webm",
          size: blob.size,
        }),
      });
      if (!presignRes.ok) throw new Error("presign failed");
      const { uploadUrl } = (await presignRes.json()) as { uploadUrl: string };
      await fetch(uploadUrl, {
        method: "PUT",
        headers: { "content-type": "video/webm" },
        body: blob,
      });
      setStatus("done");
    } catch {
      setStatus("error");
    }
  }, [sessionId]);

  const grantConsent = useCallback(async () => {
    if (
      typeof navigator === "undefined" ||
      !navigator.mediaDevices?.getDisplayMedia
    ) {
      setStatus("error");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: false,
      });
      streamRef.current = stream;

      const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp9")
        ? "video/webm;codecs=vp9"
        : "video/webm";
      const recorder = new MediaRecorder(stream, { mimeType });
      chunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      // The candidate can stop sharing via the browser's own native "Stop
      // sharing" control at any time — treat that the same as ending the
      // recording (upload whatever was captured) rather than leaving a
      // dangling stopped stream.
      stream.getVideoTracks()[0]?.addEventListener("ended", () => {
        void stopAndUpload();
      });

      recorder.start(CHUNK_MS);
      recorderRef.current = recorder;
      setStatus("recording");
    } catch {
      // Picker dismissed / permission denied — stay in "needs-consent" so
      // the prompt can be tried again, rather than silently giving up.
      setStatus("needs-consent");
    }
  }, [stopAndUpload]);

  return {
    status,
    needsConsent: status === "needs-consent",
    recording: status === "recording",
    grantConsent: () => void grantConsent(),
    stopAndUpload,
  };
}
