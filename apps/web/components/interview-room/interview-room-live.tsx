"use client";

/**
 * <InterviewRoomLive> — the REAL voice-wired room. Must be a descendant of
 * `<LiveKitRoom>`. This is the fix for "STT isn't happening": the previous
 * version of this screen only ever drove InterviewRoomFloor from a scripted
 * timer (see use-interview-demo.ts) — no mic audio was ever captured, no
 * STT ever ran. This component instead uses the SAME LiveKit hooks already
 * proven working in the original room (components/interview/live-room.tsx):
 * `useVoiceAssistant` for the agent's real state (listening/thinking/
 * speaking), `useTranscriptions` for real STT output, `useLocalParticipant`
 * for real mic control.
 *
 * Question text/progress/turn-history are NOT derived from LiveKit at all —
 * there's no per-question signal on the room (RoomMetadata only carries
 * `session_id`). Instead they're passed in as props, sourced from polling
 * `GET /api/session/{id}` in the parent (InterviewRoomClient), which reads
 * the real `context.cursor` / `context.answers` the agent updates as the
 * interview actually progresses.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  useConnectionState,
  useLocalParticipant,
  useStartAudio,
  useTranscriptions,
  useVoiceAssistant,
} from "@livekit/components-react";
import { ConnectionState, MediaDeviceFailure } from "livekit-client";
import type { AgentState } from "@livekit/components-react";
import {
  InterviewRoomFloor,
  ModalScaffold,
  type RoomPhase,
  type TurnHistoryEntry,
} from "./interview-room-floor";
import { useCameraPreview } from "./use-camera-preview";
import { useMicEnergy } from "./use-mic-energy";
import { PHButton } from "@/components/design-system";

// Same topic livekit-agents' RoomIO registers its chat handler on — typed
// (or here, button-triggered) text MUST go here to reach the agent as if
// spoken. Matches components/interview/live-room.tsx's sendText usage.
const AGENT_CHAT_TOPIC = "lk.chat";
const PAUSE_MAX = 3;

function agentStateToPhase(state: AgentState): RoomPhase {
  switch (state) {
    case "listening":
      return "listening";
    case "thinking":
      return "reviewing";
    case "speaking":
      return "asking";
    default:
      // disconnected / connecting / initializing — the agent hasn't joined
      // or hasn't spoken yet.
      return "ready";
  }
}

export interface InterviewRoomLiveProps {
  sessionId: string;
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
}

export function InterviewRoomLive({
  sessionId,
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
}: InterviewRoomLiveProps) {
  const router = useRouter();
  const connectionState = useConnectionState();
  const connected = connectionState === ConnectionState.Connected;

  const { localParticipant, isMicrophoneEnabled } = useLocalParticipant();
  const { state: agentState } = useVoiceAssistant();
  const transcriptions = useTranscriptions();
  const { mergedProps: startAudioProps, canPlayAudio } = useStartAudio({
    props: {},
  });

  const phase = agentStateToPhase(agentState);
  const floorOwner: "ai" | "candidate" | null =
    phase === "asking" || phase === "reviewing" || phase === "closing"
      ? "ai"
      : phase === "listening"
        ? "candidate"
        : null;

  const micEnergy = useMicEnergy(phase === "listening");
  const { cameraOn, cameraStream, toggleCamera } = useCameraPreview();

  // Real STT output: the most recent finalized segment from the LOCAL
  // participant (the candidate). Never fabricated — empty until the
  // candidate actually speaks and STT actually returns something.
  const transcriptText = React.useMemo(() => {
    const localIdentity = localParticipant.identity;
    let latest: { text: string; at: number } | null = null;
    for (const t of transcriptions) {
      if (t.participantInfo.identity !== localIdentity) continue;
      const text = t.text.trim();
      if (!text) continue;
      const at = t.streamInfo.timestamp ?? 0;
      if (!latest || at >= latest.at) latest = { text, at };
    }
    return latest?.text ?? "";
  }, [transcriptions, localParticipant.identity]);

  const [transcriptOpen, setTranscriptOpen] = React.useState(true);
  const [historyOpen, setHistoryOpen] = React.useState(true);
  const [fullTranscriptMode, setFullTranscriptMode] = React.useState(false);
  const [pauseCount, setPauseCount] = React.useState(PAUSE_MAX);
  const [showConfirmEnd, setShowConfirmEnd] = React.useState(false);
  const [showClosing, setShowClosing] = React.useState(false);
  const [ending, setEnding] = React.useState(false);
  const endingRef = React.useRef(false);

  /** Sends text into the SAME channel the agent's RoomIO consumes for typed
   * answers — the agent treats it as spoken, so "repeat"/"pause" are real
   * requests the (LLM-driven) interviewer actually receives and can act on,
   * not decorative buttons. */
  function sendToAgent(text: string) {
    void localParticipant
      .sendText(text, { topic: AGENT_CHAT_TOPIC })
      .catch(() => {
        // no-op if the room isn't ready
      });
  }

  function repeatQuestion() {
    sendToAgent("Sorry, could you repeat the question?");
  }

  function requestPause() {
    if (pauseCount <= 0) return;
    setPauseCount((n) => n - 1);
    sendToAgent("Could you give me a moment before I answer?");
  }

  async function endInterview() {
    if (endingRef.current) return;
    endingRef.current = true;
    setEnding(true);
    setShowConfirmEnd(false);
    setShowClosing(true);
    try {
      await localParticipant.setMicrophoneEnabled(false);
    } catch {
      // best-effort; proceed to leave regardless
    }
    // Give the closing modal a beat to actually be seen before navigating.
    await new Promise((resolve) => setTimeout(resolve, 1200));
    router.push(`/report/${encodeURIComponent(sessionId)}`);
  }

  return (
    <>
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
            onClick={() => startAudioProps.onClick()}
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
        questionText={questionText}
        transcriptText={transcriptText}
        transcriptOpen={transcriptOpen}
        onToggleTranscript={() => setTranscriptOpen((v) => !v)}
        micEnergy={micEnergy}
        micReady={connected && isMicrophoneEnabled}
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
        showCountdown={false}
        countdownValue={null}
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

/** Re-exported so the parent's onMediaDeviceFailure/onError handlers can
 * describe a mic failure the same way live-room.tsx does. */
export function describeMicFailure(failure?: MediaDeviceFailure): string {
  switch (failure) {
    case MediaDeviceFailure.PermissionDenied:
      return "Microphone access is blocked. Click the lock or mic icon in your browser's address bar, allow the microphone, then reload.";
    case MediaDeviceFailure.NotFound:
      return "No microphone was found. Connect one, then reload.";
    case MediaDeviceFailure.DeviceInUse:
      return "Your microphone is in use by another app. Close that app, then reload.";
    default:
      return "We couldn't access your microphone. Check your browser's microphone permission, then reload.";
  }
}
