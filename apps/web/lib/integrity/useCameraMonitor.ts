"use client";

/**
 * <useCameraMonitor> — webcam capture + real client-side computer vision for
 * "Camera Required" and "Camera AI: multi-face, phone, no-face, low light".
 *
 * Deliberately does NOT use LiveKit's video track — the room stays
 * `video={false}` (see live-room.tsx), keeping this isolated from the voice
 * pipeline. Requests its own `getUserMedia({video:true})` (same structural
 * status-enum/teardown pattern as <DeviceCheck>'s mic check) and never
 * publishes the stream anywhere; frames are analyzed locally and discarded.
 *
 * Detection runs entirely in-browser via two models, both loaded lazily
 * (dynamic import) only when a camera rule is actually active, so the ~several
 * MB of ML bundle never ships to a visitor who never enables these settings:
 *   - @mediapipe/tasks-vision FaceDetector → face count (no-face / multi-face)
 *   - @tensorflow-models/coco-ssd          → "cell phone" class → phone
 *   - a plain canvas luminance sample      → low light
 *
 * Known trade-off: the FaceDetector's WASM runtime + model file are fetched
 * from Google's public CDN/storage on first use (cached after). Inference
 * itself still runs locally — no video frame ever leaves the browser — but
 * that first-load fetch is an external dependency worth knowing about for a
 * self-hosted, privacy-conscious deployment. Self-hosting those assets under
 * /public is a reasonable follow-up, not done here.
 */

import { useEffect, useRef, useState } from "react";
import type { ReportViolation } from "./types";
import { isActive } from "./types";
import type { IntegrityRuleState } from "@proven-hire/shared";

export type CameraStatus =
  "idle" | "requesting" | "ok" | "denied" | "unsupported";

const CHECK_INTERVAL_MS = 1500;
const LOW_LIGHT_LUMINANCE = 40; // 0-255 scale (perceptual luma); below = "too dark"
const PHONE_CONFIDENCE = 0.5;
const REPORT_COOLDOWN_MS = 6000;
const CANVAS_W = 320;
const CANVAS_H = 240;

const FACE_MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite";
const WASM_BASE_URL =
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/wasm";

export function useCameraMonitor(
  cameraRequiredState: IntegrityRuleState | undefined,
  cameraAiState: IntegrityRuleState | undefined,
  onViolation: ReportViolation,
) {
  const needsCamera = isActive(cameraRequiredState) || isActive(cameraAiState);
  const runAi = isActive(cameraAiState);

  const [status, setStatus] = useState<CameraStatus>("idle");
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);

  // Request the camera once, whenever it's needed. Never published to the
  // LiveKit room — just an offscreen <video> element frames are sampled from.
  useEffect(() => {
    if (!needsCamera) {
      setStatus("idle");
      return;
    }
    if (
      typeof navigator === "undefined" ||
      !navigator.mediaDevices?.getUserMedia
    ) {
      setStatus("unsupported");
      return;
    }

    let cancelled = false;
    setStatus("requesting");

    navigator.mediaDevices
      .getUserMedia({ video: { width: CANVAS_W, height: CANVAS_H } })
      .then((stream) => {
        if (cancelled) {
          stream.getTracks().forEach((tr) => tr.stop());
          return;
        }
        streamRef.current = stream;
        const video = document.createElement("video");
        video.srcObject = stream;
        video.muted = true;
        video.playsInline = true;
        void video.play().catch(() => {});
        videoRef.current = video;
        setStatus("ok");
      })
      .catch(() => {
        if (!cancelled) setStatus("denied");
      });

    return () => {
      cancelled = true;
      streamRef.current?.getTracks().forEach((tr) => tr.stop());
      streamRef.current = null;
      videoRef.current = null;
    };
  }, [needsCamera]);

  // The CV loop — only once the camera is streaming AND camera-AI is active.
  useEffect(() => {
    if (status !== "ok" || !runAi) return;

    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const lastReport: Record<string, number> = {};

    const canvas = document.createElement("canvas");
    canvas.width = CANVAS_W;
    canvas.height = CANVAS_H;
    const ctx2d = canvas.getContext("2d", { willReadFrequently: true });

    function report(key: string, message: string, skipCooldown = false) {
      const now = Date.now();
      // Skip cooldown for face_detected during recovery - user needs immediate feedback
      if (!skipCooldown && now - (lastReport[key] ?? 0) < REPORT_COOLDOWN_MS) return;
      lastReport[key] = now;
      onViolation("camera_ai_detection", key, message);
    }

    async function setup() {
      const [{ FaceDetector, FilesetResolver }, coco] = await Promise.all([
        import("@mediapipe/tasks-vision"),
        import("@tensorflow-models/coco-ssd"),
        import("@tensorflow/tfjs"),
      ]);
      if (cancelled) return;

      const fileset = await FilesetResolver.forVisionTasks(WASM_BASE_URL);
      if (cancelled) return;
      const faceDetector = await FaceDetector.createFromOptions(fileset, {
        baseOptions: { modelAssetPath: FACE_MODEL_URL },
        runningMode: "VIDEO",
      });
      if (cancelled) {
        faceDetector.close();
        return;
      }
      const cocoModel = await coco.load({ base: "lite_mobilenet_v2" });
      if (cancelled) {
        faceDetector.close();
        return;
      }

      let consecutiveGoodFrames = 0;
      let lastViolationType: string | null = null;

      function tick() {
        const video = videoRef.current;
        if (!video || !ctx2d || video.readyState < 2) return;
        ctx2d.drawImage(video, 0, 0, CANVAS_W, CANVAS_H);

        const faces = faceDetector.detectForVideo(canvas, performance.now());
        const faceCount = faces.detections.length;
        if (faceCount === 0) {
          // Type names match packages/shared/data/proctoring-weights.json's
          // keys exactly (face_missing=0.25, multiple_faces=1) so these
          // reports carry real weight; phone/low_light are weight-0/logged.
          report("face_missing", "No face detected in camera view.");
          consecutiveGoodFrames = 0;
          lastViolationType = "face_missing";
        } else if (faceCount > 1) {
          report("multiple_faces", "Multiple faces detected in camera view.");
          consecutiveGoodFrames = 0;
          lastViolationType = "multiple_faces";
        } else {
          // Exactly 1 face detected - track consecutive good frames
          // If we just recovered from a violation, reset counter to re-report face_detected
          const isRecovery = lastViolationType !== null;
          if (isRecovery) {
            consecutiveGoodFrames = 0;
            lastViolationType = null;
          }
          consecutiveGoodFrames++;
          // After 3 consecutive good frames (3 * 1.5s = 4.5s), report face detected
          if (consecutiveGoodFrames === 3) {
            // Skip cooldown during recovery so modal closes immediately
            report("face_detected", "Face successfully detected in camera view.", isRecovery);
          }
        }

        void cocoModel.detect(canvas).then((preds) => {
          if (
            preds.some(
              (p) => p.class === "cell phone" && p.score > PHONE_CONFIDENCE,
            )
          ) {
            report("phone_detected", "A phone was detected in camera view.");
          }
        });

        const frame = ctx2d.getImageData(0, 0, CANVAS_W, CANVAS_H).data;
        let sum = 0;
        let n = 0;
        const stride = 4 * 8; // sample every 8th pixel — luminance doesn't need every pixel
        for (let i = 0; i < frame.length; i += stride) {
          sum +=
            0.299 * frame[i]! + 0.587 * frame[i + 1]! + 0.114 * frame[i + 2]!;
          n += 1;
        }
        const avgLuma = n > 0 ? sum / n : 255;
        if (avgLuma < LOW_LIGHT_LUMINANCE) {
          report("low_light", "Camera view is too dark.");
        }
      }

      timer = setInterval(tick, CHECK_INTERVAL_MS);
      tick();

      return () => faceDetector.close();
    }

    let cleanupModels: (() => void) | undefined;
    void setup().then((fn) => {
      if (cancelled) fn?.();
      else cleanupModels = fn;
    });

    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
      cleanupModels?.();
    };
  }, [status, runAi, onViolation]);

  return { status };
}
