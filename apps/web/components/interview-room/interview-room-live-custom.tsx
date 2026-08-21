"use client";

/**
 * <InterviewRoomLiveCustom> — the custom-transport room (Live Voice Pipeline
 * Replacement, Phase 5). No LiveKit anywhere: owns an InterviewSession (mic
 * capture, direct Deepgram STT, coordination WS to agent-api) and drives
 * InterviewRoomFloor from its floor-state machine via floorStateToPhase(),
 * replacing LiveKit's useVoiceAssistant + agentStateToPhase().
 *
 * Question text/progress/turn-history are NOT derived from the session at
 * all — unchanged from the LiveKit-based room, they're passed in as props
 * sourced from the parent's independent GET /api/session/{id} poll.
 *
 * "End interview" is deliberately just session.close() + navigate, not a
 * request routed through the LLM — the client needs a deterministic way to
 * leave regardless of what the model decides, exactly like the old room's
 * behavior (mute + navigate; the backend's own disconnect-triggered
 * persist+score handles cleanup either way). "Repeat question" / "need a
 * moment" DO go through the model (session.sendText), since those are
 * genuine conversational requests the interviewer should acknowledge.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  InterviewRoomFloor,
  ModalScaffold,
  type RoomPhase,
  type TurnHistoryEntry,
} from "./interview-room-floor";
import {
  IntegrityBanner,
  FullscreenRequiredModal,
  ProctoringBanModal,
} from "./integrity-overlay";
import { useCameraPreview } from "./use-camera-preview";
import { useMicEnergy } from "./use-mic-energy";
import { useIntegrityMonitor } from "@/lib/integrity";
import { PHButton } from "@/components/design-system";
import {
  InterviewSession,
  type FloorState,
  type InterviewSessionError,
} from "@/lib/interview-session";

const PAUSE_MAX = 3;

function floorStateToPhase(state: FloorState): RoomPhase {
  switch (state) {
    case "USER_SPEAKING":
      return "listening";
    case "AI_THINKING":
      return "reviewing";
    case "AI_SPEAKING":
      return "asking";
    default:
      // IDLE — session not yet connected, or between turns.
      return "ready";
  }
}

export interface InterviewRoomLiveCustomProps {
  sessionId: string;
  agentWsBaseUrl: string;
  candidateName: string;
  candidateInitials: string;
  role: string;
  company: string | null;
  experienceLabel?: string;
  stageLabel: string;
  questionText: string;
  questionIndex: number;
  totalQuestions: number;
  turnHistory: TurnHistoryEntry[];
  /** Connection genuinely failed (not mic permission — see the inline
   * banner below for that) — same posture as LiveKitRoom's onError /
   * onDisconnected today; the parent shows its "connection lost" screen. */
  onFatalError: (message: string) => void;
}

export function InterviewRoomLiveCustom({
  sessionId,
  agentWsBaseUrl,
  candidateName,
  candidateInitials,
  role,
  company,
  experienceLabel,
  stageLabel,
  questionText,
  questionIndex,
  totalQuestions,
  turnHistory,
  onFatalError,
}: InterviewRoomLiveCustomProps) {
  const router = useRouter();
  const sessionRef = React.useRef<InterviewSession | null>(null);

  const [floor, setFloor] = React.useState<FloorState>("IDLE");
  const [transcriptText, setTranscriptText] = React.useState("");
  // What the AI is ACTUALLY saying, from the coordination WS's own "speak"
  // messages — not the polled plan snapshot. The model routinely paraphrases
  // the planned question or asks an improvised follow-up, so the polled
  // plan text and the live audio would silently diverge; this stays exactly
  // in sync with what the candidate hears, no polling lag either.
  const [liveQuestionText, setLiveQuestionText] = React.useState("");
  const [micTrack, setMicTrack] = React.useState<MediaStreamTrack | null>(null);
  const [micReady, setMicReady] = React.useState(false);
  const [canPlayAudio, setCanPlayAudio] = React.useState(true);
  const [micFailureMsg, setMicFailureMsg] = React.useState<string | null>(null);
  const [attempt, setAttempt] = React.useState(0);

  const phase = floorStateToPhase(floor);
  const floorOwner: "ai" | "candidate" | null =
    phase === "asking" || phase === "reviewing" || phase === "closing"
      ? "ai"
      : phase === "listening"
        ? "candidate"
        : null;

  // onFatalError/router are stable-ish in practice but not guaranteed
  // referentially stable across renders — read through a ref so the
  // connect effect below doesn't need them in its dependency array.
  const onFatalErrorRef = React.useRef(onFatalError);
  onFatalErrorRef.current = onFatalError;
  const routerRef = React.useRef(router);
  routerRef.current = router;

  React.useEffect(() => {
    let cancelled = false;
    const session = new InterviewSession({
      sessionId,
      agentWsBaseUrl,
      onFloorChange: (s) => {
        if (!cancelled) setFloor(s);
      },
      onTranscript: (text) => {
        if (!cancelled) setTranscriptText(text);
      },
      onQuestionText: (text) => {
        if (!cancelled) setLiveQuestionText(text);
      },
      onAudioBlocked: () => {
        if (!cancelled) setCanPlayAudio(false);
      },
      onError: (err: InterviewSessionError) => {
        if (cancelled) return;
        if (err.kind === "mic_permission_denied") {
          setMicFailureMsg(
            "We couldn't access your microphone. Check your browser's microphone permission, then try again.",
          );
          return;
        }
        onFatalErrorRef.current(err.message);
      },
      onEnded: () => {
        if (cancelled) return;
        routerRef.current.push(`/report/${encodeURIComponent(sessionId)}`);
      },
    });
    sessionRef.current = session;

    session
      .connect()
      .then(() => {
        if (cancelled) {
          session.close();
          return;
        }
        setMicTrack(session.micTrack);
        setMicReady(true);
      })
      .catch(() => {
        // Already surfaced via the onError callback above.
      });

    return () => {
      cancelled = true;
      session.close();
      sessionRef.current = null;
    };
  }, [sessionId, agentWsBaseUrl, attempt]);

  const micEnergy = useMicEnergy(phase === "listening", micTrack);
  const { cameraOn, cameraStream, toggleCamera } = useCameraPreview();

  const [transcriptOpen, setTranscriptOpen] = React.useState(true);
  const [historyOpen, setHistoryOpen] = React.useState(true);
  const [fullTranscriptMode, setFullTranscriptMode] = React.useState(false);
  const [pauseCount, setPauseCount] = React.useState(PAUSE_MAX);
  const [showConfirmEnd, setShowConfirmEnd] = React.useState(false);
  const [showClosing, setShowClosing] = React.useState(false);
  const [ending, setEnding] = React.useState(false);
  const endingRef = React.useRef(false);

  // Purely cosmetic "get ready" countdown — same rationale as the LiveKit
  // room: the interviewer's own opening line is in flight server-side
  // (LLM/TTS latency, possible cold start). Never gates anything real.
  const [countdownValue, setCountdownValue] = React.useState<number | null>(null);
  const countdownStartedRef = React.useRef(false);
  React.useEffect(() => {
    if (!micReady || countdownStartedRef.current) return;
    countdownStartedRef.current = true;
    setCountdownValue(3);
    const interval = setInterval(() => {
      setCountdownValue((v) => {
        if (v === null || v <= 1) {
          clearInterval(interval);
          return null;
        }
        return v - 1;
      });
    }, 1000);
    return () => clearInterval(interval);
  }, [micReady]);
  React.useEffect(() => {
    if (phase !== "ready" && countdownValue !== null) setCountdownValue(null);
  }, [phase, countdownValue]);

  function repeatQuestion() {
    sessionRef.current?.sendText("Sorry, could you repeat the question?");
  }

  function requestPause() {
    if (pauseCount <= 0) return;
    setPauseCount((n) => n - 1);
    sessionRef.current?.sendText("Could you give me a moment before I answer?");
  }

  async function endInterview() {
    if (endingRef.current) return;
    endingRef.current = true;
    setEnding(true);
    setShowConfirmEnd(false);
    setShowClosing(true);
    // Deterministic close (not routed through the model) — the server's own
    // disconnect handler runs the same persist+score sequence it uses for a
    // model-driven end_interview, so this is a safe, always-works path.
    sessionRef.current?.close();
    await new Promise((resolve) => setTimeout(resolve, 1200));
    router.push(`/report/${encodeURIComponent(sessionId)}`);
  }

  const endInterviewRef = React.useRef<() => void>(() => {});
  endInterviewRef.current = () => void endInterview();
  const onAutoEnd = React.useCallback(() => endInterviewRef.current(), []);

  const {
    banner: integrityBanner,
    banned,
    needsFullscreen,
    requestFullscreen,
  } = useIntegrityMonitor(sessionId, onAutoEnd, micTrack);

  return (
    <>
      {integrityBanner && <IntegrityBanner banner={integrityBanner} />}
      {banned && <ProctoringBanModal onTimeout={onAutoEnd} />}
      {!banned && needsFullscreen && (
        <FullscreenRequiredModal onContinue={requestFullscreen} />
      )}

      {micFailureMsg && (
        <div
          role="alert"
          className="fixed inset-x-0 top-0 z-50 flex items-center justify-center gap-4 bg-[#3a1010] px-6 py-3 text-center text-[13px] text-white shadow-lg"
        >
          <span>{micFailureMsg}</span>
          <PHButton
            variant="secondary"
            onClick={() => {
              setMicFailureMsg(null);
              setAttempt((n) => n + 1);
            }}
          >
            Try again
          </PHButton>
        </div>
      )}

      {!canPlayAudio && (
        <ModalScaffold titleId="audio-unlock-title">
          <p id="audio-unlock-title" className="text-[19px] font-semibold text-white">
            Enable interview audio
          </p>
          <p className="text-[13px] leading-relaxed text-white/60">
            Your browser blocked the interviewer&rsquo;s voice from playing
            automatically. Click below so you can hear them.
          </p>
          <PHButton
            variant="primary"
            onClick={() => {
              void sessionRef.current?.unlockAudio();
              setCanPlayAudio(true);
            }}
          >
            Enable audio &amp; continue
          </PHButton>
        </ModalScaffold>
      )}

      <InterviewRoomFloor
        candidateName={candidateName}
        candidateInitials={candidateInitials}
        role={role}
        company={company}
        experienceLabel={experienceLabel}
        stageLabel={stageLabel}
        phase={phase}
        floorOwner={floorOwner}
        questionIndex={questionIndex}
        totalQuestions={totalQuestions}
        questionText={liveQuestionText || questionText}
        transcriptText={transcriptText}
        transcriptOpen={transcriptOpen}
        onToggleTranscript={() => setTranscriptOpen((v) => !v)}
        micEnergy={micEnergy}
        micReady={micReady}
        cameraOn={cameraOn}
        cameraStream={cameraStream}
        onToggleCamera={() => void toggleCamera()}
        historyOpen={historyOpen}
        onToggleHistory={() => setHistoryOpen((v) => !v)}
        fullTranscriptMode={fullTranscriptMode}
        onToggleFullTranscript={() => setFullTranscriptMode((v) => !v)}
        turnHistory={turnHistory}
        pauseCount={pauseCount}
        pauseMax={PAUSE_MAX}
        onRequestPause={requestPause}
        onRepeatQuestion={repeatQuestion}
        onEndInterview={() => setShowConfirmEnd(true)}
        ending={ending}
        showCountdown={countdownValue !== null}
        countdownValue={countdownValue}
        showConfirmEnd={showConfirmEnd}
        onConfirmEnd={() => void endInterview()}
        onCancelEnd={() => setShowConfirmEnd(false)}
        showPaused={false}
        onContinueFromPause={() => {}}
        showClosing={showClosing}
      />
    </>
  );
}
