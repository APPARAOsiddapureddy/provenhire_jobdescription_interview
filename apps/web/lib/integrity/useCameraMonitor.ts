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

// Structural types so the model modules stay dynamically imported (they are
// several MB and must not be pulled into the static bundle by a type import).
type FaceDetectorLike = {
  detectForVideo: (
    c: HTMLCanvasElement,
    ts: number,
  ) => { detections: unknown[] };
  close: () => void;
};
type PhoneModel = {
  detect: (
    c: HTMLCanvasElement,
  ) => Promise<Array<{ class: string; score: number }>>;
};
type Landmark = { x: number; y: number; z: number };
type FaceLandmarkerLike = {
  detectForVideo: (
    c: HTMLCanvasElement,
    ts: number,
  ) => { faceLandmarks: Landmark[][] };
  close: () => void;
};

// Face presence is the gate for STARTING the interview, so this loop needs
// to be responsive rather than leisurely — at the old 1500ms the candidate
// waited seconds past the moment they were actually in frame.
const CHECK_INTERVAL_MS = 600;
const LOW_LIGHT_LUMINANCE = 40; // 0-255 scale (perceptual luma); below = "too dark"
const PHONE_CONFIDENCE = 0.5;
const REPORT_COOLDOWN_MS = 6000;
const CANVAS_W = 320;
const CANVAS_H = 240;

const FACE_MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite";
const WASM_BASE_URL =
  "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/wasm";
const FACE_LANDMARK_MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";

/* ── Head-pose ("looking away") analysis ────────────────────────────────
 * HEAD POSE, deliberately not eye gaze. At this canvas resolution the eye
 * region is only a few pixels across, so pupil-derived gaze is mostly
 * noise, and eyes dart constantly in ways that mean nothing — a candidate
 * flicking their eyes to their own transcript panel would flag. Head yaw/
 * pitch is a large, stable measurement that survives poor lighting and
 * glasses. The accepted cost: someone facing the camera while reading notes
 * out of the corner of their eye is missed. That miss is preferable to
 * flagging every honest candidate who glances sideways.
 *
 * Pose comes from landmark GEOMETRY rather than the facial transformation
 * matrix, whose row/column-major convention is easy to get subtly wrong and
 * hard to notice. Both proxies are normalised by interocular distance,
 * making them invariant to how far the candidate sits from the camera.
 */
// MediaPipe FaceMesh canonical indices.
const LM_NOSE_TIP = 1;
const LM_EYE_OUTER_A = 33;
const LM_EYE_OUTER_B = 263;

// Deviation from the candidate's OWN calibrated baseline before a frame
// counts as "looking away". Starting values — the diagnostic log below
// reports observed deviations so these can be tuned from real hardware
// rather than guessed at.
const YAW_AWAY_THRESHOLD = 0.18;
const PITCH_AWAY_THRESHOLD = 0.22;

// Calibration: the median of this many stable single-face samples becomes
// the candidate's "looking at the screen" zero point. Median, not mean, so
// a few frames where they glanced away mid-calibration cannot skew it.
// Required because camera placement varies enormously — a side-clipped or
// bottom-bezel webcam means looking AT THE SCREEN is not looking AT THE
// CAMERA, and an uncalibrated detector would flag such a candidate for the
// entire interview.
const CALIBRATION_SAMPLES = 12;

// A rolling DUTY CYCLE, not a consecutive-seconds timer. One long look away
// is ordinary thinking behaviour (people look up to recall things); a
// sustained pattern of looking at the same off-screen spot is the actual
// signal. "What fraction of the last ~30s was away" measures the latter;
// "N unbroken seconds" measures the former and fires on honest candidates
// mid-thought, exactly when they are concentrating hardest.
const GAZE_WINDOW_SAMPLES = 50; // ~30s at CHECK_INTERVAL_MS
const GAZE_AWAY_RATIO = 0.5;
const GAZE_REPORT_COOLDOWN_MS = 60_000;
const GAZE_DIAGNOSTIC_LOG_MS = 5000;

export function useCameraMonitor(
  cameraRequiredState: IntegrityRuleState | undefined,
  cameraAiState: IntegrityRuleState | undefined,
  onViolation: ReportViolation,
  gazeState?: IntegrityRuleState,
  /** True while the AI is speaking. Head-pose sampling pauses then — there
   * is no reason to police where a candidate looks while they are listening
   * rather than answering. */
  aiIsSpeaking = false,
) {
  const needsCamera = isActive(cameraRequiredState) || isActive(cameraAiState);
  const runAi = isActive(cameraAiState);
  const runGaze = isActive(gazeState);

  const [status, setStatus] = useState<CameraStatus>("idle");
  const [modelsReady, setModelsReady] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const faceDetectorRef = useRef<FaceDetectorLike | null>(null);
  const cocoModelRef = useRef<PhoneModel | null>(null);
  const landmarkerRef = useRef<FaceLandmarkerLike | null>(null);
  const aiIsSpeakingRef = useRef(aiIsSpeaking);
  aiIsSpeakingRef.current = aiIsSpeaking;

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

  // Model loading runs in PARALLEL with the camera request above, not after
  // it. Previously the whole CV effect was gated on `status === "ok"`, which
  // made startup strictly serial: settings fetch → camera permission → only
  // THEN begin downloading the MediaPipe WASM runtime + face model from the
  // CDN → first detection. Those megabytes have nothing to do with the
  // camera, so waiting for permission before starting them added the entire
  // download to the candidate's perceived "detecting face..." wait.
  useEffect(() => {
    if (!runAi) return;
    let cancelled = false;
    let detector: FaceDetectorLike | null = null;

    void (async () => {
      try {
        const { FaceDetector, FilesetResolver } =
          await import("@mediapipe/tasks-vision");
        if (cancelled) return;
        const fileset = await FilesetResolver.forVisionTasks(WASM_BASE_URL);
        if (cancelled) return;
        const created = (await FaceDetector.createFromOptions(fileset, {
          baseOptions: { modelAssetPath: FACE_MODEL_URL },
          runningMode: "VIDEO",
        })) as unknown as FaceDetectorLike;
        if (cancelled) {
          created.close();
          return;
        }
        detector = created;
        faceDetectorRef.current = created;
        setModelsReady(true);
      } catch {
        // Leave modelsReady false — the loop simply never starts rather than
        // the hook throwing during an interview.
      }
    })();

    // Phone detection (coco-ssd + the tfjs runtime) is a much larger
    // download and is NOT part of the entry gate — it warms up separately
    // and starts participating once ready.
    void (async () => {
      try {
        const [coco] = await Promise.all([
          import("@tensorflow-models/coco-ssd"),
          import("@tensorflow/tfjs"),
        ]);
        if (cancelled) return;
        cocoModelRef.current = (await coco.load({
          base: "lite_mobilenet_v2",
        })) as unknown as PhoneModel;
      } catch {
        // Best-effort; face/low-light checks continue without it.
      }
    })();

    return () => {
      cancelled = true;
      faceDetectorRef.current = null;
      cocoModelRef.current = null;
      setModelsReady(false);
      detector?.close();
    };
  }, [runAi]);

  // FaceLandmarker (head pose) loads ONLY when gaze analysis is enabled —
  // it is a second, heavier face model than the FaceDetector above and must
  // not be downloaded by deployments that never turn this on. Kept separate
  // from the face-count detector so enabling gaze cannot regress the
  // existing, working presence checks.
  useEffect(() => {
    if (!runGaze) return;
    let cancelled = false;
    let landmarker: FaceLandmarkerLike | null = null;

    void (async () => {
      try {
        const { FaceLandmarker, FilesetResolver } =
          await import("@mediapipe/tasks-vision");
        if (cancelled) return;
        const fileset = await FilesetResolver.forVisionTasks(WASM_BASE_URL);
        if (cancelled) return;
        const created = (await FaceLandmarker.createFromOptions(fileset, {
          baseOptions: { modelAssetPath: FACE_LANDMARK_MODEL_URL },
          runningMode: "VIDEO",
          numFaces: 1,
        })) as unknown as FaceLandmarkerLike;
        if (cancelled) {
          created.close();
          return;
        }
        landmarker = created;
        landmarkerRef.current = created;
        console.info("[gaze] head-pose model ready");
      } catch {
        // Best-effort: presence / phone / low-light checks continue without it.
      }
    })();

    return () => {
      cancelled = true;
      landmarkerRef.current = null;
      landmarker?.close();
    };
  }, [runGaze]);

  // The CV loop — once the camera is streaming AND the face model is loaded.
  useEffect(() => {
    if (status !== "ok" || !runAi || !modelsReady) return;
    const faceDetector = faceDetectorRef.current;
    if (!faceDetector) return;

    let timer: ReturnType<typeof setInterval> | null = null;
    const lastReport: Record<string, number> = {};

    const canvas = document.createElement("canvas");
    canvas.width = CANVAS_W;
    canvas.height = CANVAS_H;
    const ctx2d = canvas.getContext("2d", { willReadFrequently: true });

    function report(key: string, message: string, skipCooldown = false) {
      const now = Date.now();
      // Skip cooldown for face_detected during recovery - user needs immediate feedback
      if (!skipCooldown && now - (lastReport[key] ?? 0) < REPORT_COOLDOWN_MS)
        return;
      lastReport[key] = now;
      onViolation("camera_ai_detection", key, message);
    }

    let consecutiveGoodFrames = 0;
    let lastViolationType: string | null = null;

    // --- head-pose state -------------------------------------------------
    const calibrationYaw: number[] = [];
    const calibrationPitch: number[] = [];
    let baseline: { yaw: number; pitch: number } | null = null;
    const awayWindow: number[] = [];
    let lastGazeReportAt = 0;
    let lastGazeLogAt = Date.now();

    function median(values: number[]): number {
      const sorted = [...values].sort((a, b) => a - b);
      const mid = Math.floor(sorted.length / 2);
      return sorted.length % 2 === 0
        ? ((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2
        : (sorted[mid] ?? 0);
    }

    /** Called only from the faceCount === 1 branch below — head pose is
     * meaningless with zero or several faces in frame. */
    function sampleHeadPose() {
      const landmarker = landmarkerRef.current;
      if (!landmarker || !ctx2d) return;

      // Paused while the AI talks: the candidate is listening, not
      // answering, and where they look then carries no signal.
      if (aiIsSpeakingRef.current) return;

      let pts: Landmark[] | undefined;
      try {
        pts = landmarker.detectForVideo(canvas, performance.now())
          .faceLandmarks[0];
      } catch {
        return;
      }
      const nose = pts?.[LM_NOSE_TIP];
      const eyeA = pts?.[LM_EYE_OUTER_A];
      const eyeB = pts?.[LM_EYE_OUTER_B];
      if (!nose || !eyeA || !eyeB) return;

      // Interocular distance normalises both proxies for how close the
      // candidate is sitting, so the same threshold works at any distance.
      const interocular = Math.hypot(eyeB.x - eyeA.x, eyeB.y - eyeA.y);
      if (interocular < 1e-6) return;
      const eyeMidX = (eyeA.x + eyeB.x) / 2;
      const eyeMidY = (eyeA.y + eyeB.y) / 2;
      const yaw = (nose.x - eyeMidX) / interocular;
      const pitch = (nose.y - eyeMidY) / interocular;

      if (!baseline) {
        calibrationYaw.push(yaw);
        calibrationPitch.push(pitch);
        if (calibrationYaw.length >= CALIBRATION_SAMPLES) {
          baseline = {
            yaw: median(calibrationYaw),
            pitch: median(calibrationPitch),
          };
          console.info("[gaze] calibrated neutral head pose", {
            yaw: baseline.yaw.toFixed(3),
            pitch: baseline.pitch.toFixed(3),
          });
        }
        return; // never evaluate against an un-calibrated baseline
      }

      const dYaw = Math.abs(yaw - baseline.yaw);
      const dPitch = Math.abs(pitch - baseline.pitch);
      const away =
        dYaw > YAW_AWAY_THRESHOLD || dPitch > PITCH_AWAY_THRESHOLD ? 1 : 0;

      awayWindow.push(away);
      if (awayWindow.length > GAZE_WINDOW_SAMPLES) awayWindow.shift();

      const ratio =
        awayWindow.length > 0
          ? awayWindow.reduce((a, b) => a + b, 0) / awayWindow.length
          : 0;

      const now = Date.now();
      if (now - lastGazeLogAt >= GAZE_DIAGNOSTIC_LOG_MS) {
        console.info("[gaze] head pose", {
          dYaw: dYaw.toFixed(3),
          dPitch: dPitch.toFixed(3),
          away: Boolean(away),
          awayRatio: ratio.toFixed(2),
          window: `${awayWindow.length}/${GAZE_WINDOW_SAMPLES}`,
        });
        lastGazeLogAt = now;
      }

      // Judge only on a FULL window — a partly-filled one would let a couple
      // of early away-frames read as 100%.
      if (
        awayWindow.length >= GAZE_WINDOW_SAMPLES &&
        ratio >= GAZE_AWAY_RATIO &&
        now - lastGazeReportAt > GAZE_REPORT_COOLDOWN_MS
      ) {
        lastGazeReportAt = now;
        const pct = Math.round(ratio * 100);
        console.warn("[gaze] sustained look-away logged", { awayRatio: ratio });
        // silent: recorded for the reviewer, never surfaced to the candidate.
        onViolation(
          "gaze_detection",
          "gaze_away_sustained",
          `Candidate looked away from the screen for ${pct}% of the last ~30 seconds.`,
          { silent: true, severity: "info" },
        );
        awayWindow.length = 0; // start a fresh window after each report
      }
    }

    function tick() {
      const video = videoRef.current;
      if (!video || !ctx2d || video.readyState < 2) return;
      ctx2d.drawImage(video, 0, 0, CANVAS_W, CANVAS_H);

      const faces = faceDetector!.detectForVideo(canvas, performance.now());
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
        // 2 consecutive good frames (~1.2s) — enough to reject a single
        // fluke detection without making the candidate wait.
        if (consecutiveGoodFrames === 2) {
          // Skip cooldown during recovery so modal closes immediately
          report(
            "face_detected",
            "Face successfully detected in camera view.",
            isRecovery,
          );
        }
        // Exactly one face in frame — the only state where head pose is
        // meaningful.
        sampleHeadPose();
      }

      const cocoModel = cocoModelRef.current;
      if (cocoModel) {
        void cocoModel.detect(canvas).then((preds) => {
          if (
            preds.some(
              (p) => p.class === "cell phone" && p.score > PHONE_CONFIDENCE,
            )
          ) {
            report("phone_detected", "A phone was detected in camera view.");
          }
        });
      }

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

    return () => {
      if (timer) clearInterval(timer);
    };
    // The detector itself is owned (and closed) by the loading effect above.
    // runGaze is a dependency so toggling gaze mid-session restarts the loop
    // with fresh calibration and window state.
  }, [status, runAi, runGaze, modelsReady, onViolation]);

  return { status };
}
