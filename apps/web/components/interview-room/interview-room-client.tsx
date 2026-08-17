"use client";

/**
 * <InterviewRoomClient> — bridges REAL session data (polled from the prep
 * pipeline, same `fetchSessionView` poller the existing /session/[id] prep
 * screen uses) into the interview room.
 *
 * Two paths, matching components/interview/live-room.tsx's established
 * split:
 *  • LIVE  (token && url, LiveKit configured): wraps <InterviewRoomLive> in
 *    <LiveKitRoom> — real STT, real agent voice, real mic control. This is
 *    the path that actually fixes "STT isn't happening": the room genuinely
 *    connects and captures audio now.
 *  • DEMO  (LiveKit not configured): falls back to the scripted
 *    useInterviewDemo walkthrough, so the screen still shows something
 *    coherent in an offline/no-keys build instead of being unusable.
 *
 * Question text/progress/turn-history come from POLLING `GET
 * /api/session/{id}` in both paths (real `context.cursor` / `context.
 * answers`, updated server-side as the interview actually progresses) —
 * LiveKit's RoomMetadata only carries `session_id`, nothing per-question.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  LiveKitRoom,
  RoomAudioRenderer,
} from "@livekit/components-react";
import { DisconnectReason, MediaDeviceFailure } from "livekit-client";
import { fetchSessionView, resetSessionPolling } from "@/lib/session";
import type { ClientSessionView } from "@/lib/session";
import {
  InterviewRoomFloor,
  type TurnHistoryEntry,
} from "./interview-room-floor";
import { InterviewRoomLive, describeMicFailure } from "./interview-room-live";
import { useInterviewDemo, type DemoQuestion } from "./use-interview-demo";
import { useCameraPreview } from "./use-camera-preview";
import { PHButton, PHLogo } from "@/components/design-system";

const STAGE_BY_SECTION: Record<string, string> = {
  intro: "Role Fit",
  behavioral: "Role Fit",
  technical: "Core Skills from JD",
  coding: "Scenario Thinking",
  wrap: "Scenario Thinking",
};

// Keeps polling for the life of the room (not just until first ready) —
// question index/turn history need to track REAL progress as the live
// interview advances. Stops on unmount or once the session reaches a
// terminal status.
const POLL_MS = 4000;
const TERMINAL_STATUSES = new Set([
  "complete",
  "no_answers",
  "error",
  "rejected",
  "not_found",
  "stalled",
]);

function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const first = parts[0]?.[0] ?? "";
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? "") : "";
  return (first + last).toUpperCase();
}

function LoadingScaffold({ children }: { children: React.ReactNode }) {
  return (
    <main className="ph-shell relative flex min-h-screen items-center justify-center bg-[#020304] px-6">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(50% 40% at 50% 0%, oklch(0.5 0.15 255 / 0.18), transparent 70%), linear-gradient(180deg, #071014, #020304 82%)",
        }}
      />
      <div className="ph-card relative flex max-w-sm flex-col items-center gap-4 rounded-room px-10 py-10 text-center">
        <PHLogo compact />
        {children}
      </div>
    </main>
  );
}

export function InterviewRoomClient({
  sessionId,
  candidateNameHint,
  roleHint,
  experienceLabel,
  token,
  url,
}: {
  sessionId: string;
  candidateNameHint: string;
  roleHint: string;
  experienceLabel?: string;
  /** Null when LiveKit is unconfigured → falls back to the scripted demo. */
  token: string | null;
  url: string | null;
}) {
  const router = useRouter();
  const [view, setView] = React.useState<ClientSessionView | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    resetSessionPolling(sessionId);

    async function poll() {
      const v = await fetchSessionView(sessionId);
      if (cancelled) return;
      setView(v);
      if (TERMINAL_STATUSES.has(v.status)) clearInterval(interval);
    }

    void poll();
    const interval = setInterval(() => {
      if (!cancelled) void poll();
    }, POLL_MS);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [sessionId]);

  const ready = Boolean(view?.context);
  const failed =
    view?.status === "not_found" ||
    view?.status === "stalled" ||
    view?.status === "rejected" ||
    (view?.status === "error" && !view.context);

  const planQuestions = view?.context?.plan.questions ?? [];
  const cursor = view?.context?.cursor ?? 0;
  const currentQuestion = planQuestions[Math.min(cursor, planQuestions.length - 1)];
  const stageLabel = currentQuestion
    ? (STAGE_BY_SECTION[currentQuestion.section] ?? "Role Fit")
    : "Role Fit";

  // Real turn history: every question the agent has actually recorded an
  // answer for, paired from context.answers (question_id → transcript).
  const turnHistory: TurnHistoryEntry[] = React.useMemo(() => {
    if (!view?.context) return [];
    const answerByQ = new Map(
      view.context.answers.map((a) => [a.question_id, a.transcript]),
    );
    return view.context.plan.questions
      .filter((q) => answerByQ.has(q.id))
      .map((q) => ({
        id: q.id,
        question: q.text.en ?? "",
        answer: answerByQ.get(q.id) ?? null,
      }));
  }, [view]);

  const candidateName =
    view?.context?.candidate.name?.trim() || candidateNameHint || "Candidate";
  const role = view?.context?.job.title?.trim() || roleHint || "this role";
  const company = view?.context?.job.company_name || null;

  const canConnect = Boolean(token && url);

  // Guard the browser-only LiveKit client from SSR / first hydration —
  // same reasoning as components/interview/live-room.tsx.
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);

  const [connectionLost, setConnectionLost] = React.useState<string | null>(
    null,
  );
  const [micFailureMsg, setMicFailureMsg] = React.useState<string | null>(
    null,
  );
  const [joinAttempt, setJoinAttempt] = React.useState(0);

  const handleError = React.useCallback((error: Error) => {
    const failure = MediaDeviceFailure.getFailure(error);
    if (failure) return; // surfaced via the room's "Mic muted" pill instead
    setConnectionLost(
      "We couldn't reach the interview room. Check your network, then rejoin.",
    );
  }, []);

  const handleDisconnected = React.useCallback(
    (reason?: DisconnectReason) => {
      if (reason === DisconnectReason.CLIENT_INITIATED) return;
      if (reason === DisconnectReason.ROOM_DELETED) {
        router.push(`/report/${encodeURIComponent(sessionId)}`);
        return;
      }
      setConnectionLost(
        "The connection to the interview dropped. Rejoin to pick up where you left off.",
      );
    },
    [router, sessionId],
  );

  // --- DEMO fallback path (LiveKit not configured) ---------------------
  const demoQuestions: DemoQuestion[] = React.useMemo(() => {
    if (!view?.context) return [];
    return view.context.plan.questions.map((q) => ({
      id: q.id,
      text: q.text.en ?? "",
      stageLabel: STAGE_BY_SECTION[q.section] ?? "Role Fit",
    }));
  }, [view]);
  const { cameraOn, cameraStream, toggleCamera } = useCameraPreview();
  const onFinish = React.useCallback(() => {
    router.push(`/report/${encodeURIComponent(sessionId)}`);
  }, [router, sessionId]);
  const demo = useInterviewDemo(demoQuestions, onFinish);
  const [historyOpen, setHistoryOpen] = React.useState(true);
  const [fullTranscriptMode, setFullTranscriptMode] = React.useState(false);

  if (failed) {
    return (
      <LoadingScaffold>
        <p className="text-[16px] font-medium text-white">
          We couldn&rsquo;t load this interview
        </p>
        <p className="text-[13px] leading-relaxed text-white/60">
          The session may have expired or the prep pipeline hit an error.
        </p>
        <PHButton href="/setup" variant="primary">
          Start a new interview
        </PHButton>
      </LoadingScaffold>
    );
  }

  if (!ready) {
    return (
      <LoadingScaffold>
        <p className="text-[16px] font-medium text-white">
          Preparing your interview…
        </p>
        <p className="text-[13px] leading-relaxed text-white/60">
          Reading your resume and the job description for{" "}
          {roleHint || "this role"}.
        </p>
        <span
          className="h-6 w-6 animate-spin rounded-full border-2 border-[var(--ph-blue)] border-t-transparent"
          aria-hidden
        />
      </LoadingScaffold>
    );
  }

  if (connectionLost) {
    return (
      <LoadingScaffold>
        <p className="text-[16px] font-medium text-white">Connection lost</p>
        <p className="text-[13px] leading-relaxed text-white/60">
          {connectionLost}
        </p>
        <PHButton
          variant="primary"
          onClick={() => {
            setConnectionLost(null);
            setJoinAttempt((n) => n + 1);
          }}
        >
          Rejoin interview
        </PHButton>
      </LoadingScaffold>
    );
  }

  if (canConnect) {
    if (!mounted) {
      return (
        <LoadingScaffold>
          <p className="text-[16px] font-medium text-white">Connecting…</p>
        </LoadingScaffold>
      );
    }
    return (
      <LiveKitRoom
        key={joinAttempt}
        serverUrl={url ?? undefined}
        token={token ?? undefined}
        connect
        audio
        video={false}
        data-session={sessionId}
        onError={handleError}
        onMediaDeviceFailure={(failure) => {
          // Mic capture failed (permission denied / no device / device busy).
          // The room's "Mic muted" pill shows the state but has no fix built
          // in, so without this the candidate is stuck seeing questions with
          // no way to answer and no explanation why.
          setMicFailureMsg(describeMicFailure(failure));
        }}
        onDisconnected={handleDisconnected}
      >
        <RoomAudioRenderer />
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
                setJoinAttempt((n) => n + 1);
              }}
            >
              Try again
            </PHButton>
          </div>
        )}
        <InterviewRoomLive
          sessionId={sessionId}
          candidateName={candidateName}
          candidateInitials={initialsOf(candidateName)}
          role={role}
          company={company}
          experienceLabel={experienceLabel}
          stageLabel={stageLabel}
          questionText={currentQuestion?.text.en ?? ""}
          questionIndex={cursor}
          totalQuestions={planQuestions.length}
          turnHistory={turnHistory}
        />
      </LiveKitRoom>
    );
  }

  // LiveKit isn't configured — scripted demo walkthrough (offline-friendly
  // fallback, same philosophy as live-room.tsx's PreviewSession).
  if (demoQuestions.length === 0) {
    return (
      <LoadingScaffold>
        <p className="text-[16px] font-medium text-white">
          Preparing your interview…
        </p>
      </LoadingScaffold>
    );
  }

  return (
    <InterviewRoomFloor
      candidateName={candidateName}
      candidateInitials={initialsOf(candidateName)}
      role={role}
      company={company}
      experienceLabel={experienceLabel}
      stageLabel={demo.stageLabel}
      phase={demo.phase}
      floorOwner={demo.floorOwner}
      questionIndex={demo.questionIndex}
      totalQuestions={demo.totalQuestions}
      questionText={demo.questionText}
      transcriptText=""
      transcriptOpen={demo.transcriptOpen}
      onToggleTranscript={demo.toggleTranscript}
      micEnergy={demo.micEnergy}
      micReady
      cameraOn={cameraOn}
      cameraStream={cameraStream}
      onToggleCamera={() => void toggleCamera()}
      historyOpen={historyOpen}
      onToggleHistory={() => setHistoryOpen((v) => !v)}
      fullTranscriptMode={fullTranscriptMode}
      onToggleFullTranscript={() => setFullTranscriptMode((v) => !v)}
      turnHistory={demo.turnHistory}
      pauseCount={demo.pauseCount}
      pauseMax={demo.pauseMax}
      onRequestPause={demo.requestPause}
      onRepeatQuestion={demo.repeatQuestion}
      onEndInterview={demo.requestEnd}
      ending={demo.ending}
      showCountdown={demo.showCountdown}
      countdownValue={demo.countdownValue}
      showConfirmEnd={demo.showConfirmEnd}
      onConfirmEnd={demo.confirmEnd}
      onCancelEnd={demo.cancelEnd}
      showPaused={demo.showPaused}
      onContinueFromPause={demo.continueFromPause}
      showClosing={demo.showClosing}
    />
  );
}
