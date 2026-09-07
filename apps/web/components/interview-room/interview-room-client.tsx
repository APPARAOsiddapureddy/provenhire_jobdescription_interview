"use client";

/**
 * <InterviewRoomClient> — bridges REAL session data (polled from the prep
 * pipeline, same `fetchSessionView` poller the existing /session/[id] prep
 * screen uses) into the interview room.
 *
 * Custom transport only (Live Voice Pipeline Replacement, hard cutover): no
 * LiveKit, no demo-fallback branch. The old fallback existed for "LiveKit
 * isn't configured"; the new architecture has no equivalent client-visible
 * config flag (Deepgram/OpenAI/Cartesia keys live only in the agent's own
 * env, never exposed to the browser), so a misconfigured deployment now
 * surfaces as a real connection error instead of silently degrading to a
 * scripted walkthrough — matches deepgram-token/route.ts's own "no safe
 * mock fallback" posture for this exact reason.
 *
 * Question text/progress/turn-history come from POLLING `GET
 * /api/session/{id}` (real `context.cursor` / `context.answers`, updated
 * server-side as the interview actually progresses) — the coordination WS
 * carries no per-question state of its own.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { fetchSessionView, resetSessionPolling } from "@/lib/session";
import type { ClientSessionView } from "@/lib/session";
import type { TurnHistoryEntry } from "./interview-room-floor";
import { InterviewRoomLiveCustom } from "./interview-room-live-custom";
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
  agentWsBaseUrl,
}: {
  sessionId: string;
  candidateNameHint: string;
  roleHint: string;
  experienceLabel?: string;
  /** wss:// origin the browser connects the coordination WS to directly —
   * derived server-side from AGENT_API_URL. */
  agentWsBaseUrl: string;
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
  const currentQuestion =
    planQuestions[Math.min(cursor, planQuestions.length - 1)];
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

  // Guard the browser-only InterviewSession from SSR / first hydration
  // (getUserMedia/AudioContext/WebSocket don't exist server-side).
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);

  const [connectionLost, setConnectionLost] = React.useState<string | null>(
    null,
  );
  const [joinAttempt, setJoinAttempt] = React.useState(0);

  const handleFatalError = React.useCallback((message: string) => {
    setConnectionLost(message);
  }, []);

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

  if (!mounted) {
    return (
      <LoadingScaffold>
        <p className="text-[16px] font-medium text-white">Connecting…</p>
      </LoadingScaffold>
    );
  }

  return (
    <InterviewRoomLiveCustom
      key={joinAttempt}
      sessionId={sessionId}
      agentWsBaseUrl={agentWsBaseUrl}
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
      onFatalError={handleFatalError}
    />
  );
}
