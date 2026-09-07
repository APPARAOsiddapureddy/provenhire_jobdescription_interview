"use client";

/**
 * useInterviewDemo — drives InterviewRoomFloor's phase state machine on a
 * script, using REAL JD-derived questions from the prep pipeline.
 *
 * IMPORTANT scope note: this is a scripted (timer-driven) walk through the
 * room's phases — it is NOT wired to the real LiveKit voice pipeline. No
 * candidate speech is ever captured or fabricated here: `turnHistory` always
 * records `answer: null`, and the live-transcript panel's honest empty state
 * ("Waiting for the candidate…") is left alone rather than faked. Wiring
 * this room to real voice is a separate, larger integration a follow-up
 * should do explicitly — this hook exists to demonstrate the flagship
 * visual/motion system against real interview content.
 */

import * as React from "react";
import type { RoomPhase, TurnHistoryEntry } from "./interview-room-floor";

export interface DemoQuestion {
  id: string;
  text: string;
  stageLabel: string;
}

const ASK_MS = 4500;
const LISTEN_MS = 6000;
const REVIEW_MS = 2200;
const COUNTDOWN_FROM = 3;
const PAUSE_MAX = 3;

type Step =
  | { kind: "waiting" }
  | { kind: "countdown"; value: number }
  | { kind: "ask"; index: number }
  | { kind: "listen"; index: number }
  | { kind: "review"; index: number }
  | { kind: "closing" };

export function useInterviewDemo(
  questions: DemoQuestion[],
  onFinish: () => void,
) {
  // Hooks can't be called conditionally, so this hook always runs from the
  // moment InterviewRoomClient mounts — even while the real prep pipeline is
  // still working and `questions` is still `[]`. Without an explicit
  // "waiting" state, the clock below would start counting down against an
  // empty question list and race straight to "closing" long before real
  // questions arrive (this actually happened in testing: ~18s from mount to
  // an auto-navigate to /report with zero questions asked). Only once real
  // questions land does the countdown — and the rest of the script — begin.
  const [step, setStep] = React.useState<Step>({ kind: "waiting" });
  const [micEnergy, setMicEnergy] = React.useState(0);
  const [transcriptOpen, setTranscriptOpen] = React.useState(true);
  const [turnHistory, setTurnHistory] = React.useState<TurnHistoryEntry[]>([]);
  const [pauseCount, setPauseCount] = React.useState(PAUSE_MAX);
  const [paused, setPaused] = React.useState(false);
  const [showConfirmEnd, setShowConfirmEnd] = React.useState(false);
  const [ending, setEnding] = React.useState(false);

  const timeoutRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const micRafRef = React.useRef<number | null>(null);
  const questionsRef = React.useRef(questions);
  questionsRef.current = questions;
  const onFinishRef = React.useRef(onFinish);
  onFinishRef.current = onFinish;

  const clearScheduled = React.useCallback(() => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = null;
  }, []);

  const schedule = React.useCallback(
    (ms: number, next: Step) => {
      clearScheduled();
      timeoutRef.current = setTimeout(() => {
        setStep(next);
      }, ms);
    },
    [clearScheduled],
  );

  // Once real questions land, start the countdown. Runs once per genuine
  // 0 → N transition (guarded by `step.kind === "waiting"` so it can't
  // re-fire and restart an already-running script).
  React.useEffect(() => {
    if (step.kind === "waiting" && questions.length > 0) {
      setStep({ kind: "countdown", value: COUNTDOWN_FROM });
    }
  }, [questions.length, step.kind]);

  // Drive the state machine forward whenever `step` changes (unless paused
  // or the confirm-end modal is open — both freeze the clock).
  React.useEffect(() => {
    if (paused || showConfirmEnd || ending || step.kind === "waiting") return;
    const total = questionsRef.current.length;

    if (step.kind === "countdown") {
      if (step.value <= 1) {
        schedule(1000, { kind: "ask", index: 0 });
      } else {
        schedule(1000, { kind: "countdown", value: step.value - 1 });
      }
      return;
    }
    if (step.kind === "ask") {
      schedule(ASK_MS, { kind: "listen", index: step.index });
      return;
    }
    if (step.kind === "listen") {
      schedule(LISTEN_MS, { kind: "review", index: step.index });
      return;
    }
    if (step.kind === "review") {
      const q = questionsRef.current[step.index];
      if (q) {
        // Repeating a question during ITS OWN review phase (the Repeat
        // button stays enabled then) sends that same index back through
        // "ask" → … → "review" again — de-dupe by id so it doesn't land in
        // the question log twice.
        setTurnHistory((prev) =>
          prev.some((t) => t.id === q.id)
            ? prev
            : [...prev, { id: q.id, question: q.text, answer: null }],
        );
      }
      const next = step.index + 1;
      if (next < total) {
        schedule(REVIEW_MS, { kind: "ask", index: next });
      } else {
        schedule(REVIEW_MS, { kind: "closing" });
      }
      return;
    }
    if (step.kind === "closing") {
      const t = setTimeout(() => {
        setEnding(true);
        onFinishRef.current();
      }, 1800);
      return () => clearTimeout(t);
    }
  }, [step, paused, showConfirmEnd, ending, schedule]);

  // Synthetic mic-energy waveform while "listening" — a visual affordance
  // only, never presented as a real transcript.
  React.useEffect(() => {
    if (step.kind !== "listen" || paused) {
      setMicEnergy(0);
      return;
    }
    let t0 = performance.now();
    function tick(now: number) {
      const elapsed = (now - t0) / 1000;
      const wave =
        0.5 + 0.35 * Math.sin(elapsed * 2.4) + 0.15 * Math.sin(elapsed * 7.1);
      setMicEnergy(Math.max(0, Math.min(1, wave)));
      micRafRef.current = requestAnimationFrame(tick);
    }
    micRafRef.current = requestAnimationFrame(tick);
    return () => {
      if (micRafRef.current) cancelAnimationFrame(micRafRef.current);
    };
  }, [step, paused]);

  React.useEffect(() => clearScheduled, [clearScheduled]);

  const currentIndex =
    step.kind === "ask" || step.kind === "listen" || step.kind === "review"
      ? step.index
      : step.kind === "closing"
        ? questions.length
        : 0;

  const phase: RoomPhase =
    step.kind === "waiting" || step.kind === "countdown"
      ? "ready"
      : step.kind === "ask"
        ? "asking"
        : step.kind === "listen"
          ? "listening"
          : step.kind === "review"
            ? "reviewing"
            : "closing";

  const floorOwner: "ai" | "candidate" | null =
    phase === "asking" || phase === "reviewing" || phase === "closing"
      ? "ai"
      : phase === "listening"
        ? "candidate"
        : null;

  const current = questions[Math.min(currentIndex, questions.length - 1)];

  function repeatQuestion() {
    if (step.kind === "listen" || step.kind === "review") {
      setStep({ kind: "ask", index: step.index });
    } else if (step.kind === "ask") {
      setStep({ kind: "ask", index: step.index });
    }
  }

  function requestPause() {
    if (pauseCount <= 0) return;
    setPauseCount((n) => n - 1);
    setPaused(true);
    clearScheduled();
  }

  function continueFromPause() {
    setPaused(false);
  }

  function requestEnd() {
    setShowConfirmEnd(true);
    clearScheduled();
  }

  function cancelEnd() {
    setShowConfirmEnd(false);
    // Re-arm the current step so the clock resumes from where it paused.
    setStep((s) => ({ ...s }));
  }

  function confirmEnd() {
    setShowConfirmEnd(false);
    setEnding(true);
    clearScheduled();
    onFinishRef.current();
  }

  return {
    phase,
    floorOwner,
    questionIndex: Math.min(currentIndex, Math.max(questions.length - 1, 0)),
    totalQuestions: questions.length,
    questionText: current?.text ?? "",
    stageLabel: current?.stageLabel ?? "",
    micEnergy,
    transcriptOpen,
    toggleTranscript: () => setTranscriptOpen((v) => !v),
    turnHistory,
    pauseCount,
    pauseMax: PAUSE_MAX,
    requestPause,
    continueFromPause,
    repeatQuestion,
    requestEnd,
    confirmEnd,
    cancelEnd,
    showCountdown: step.kind === "countdown",
    countdownValue: step.kind === "countdown" ? step.value : null,
    showConfirmEnd,
    showPaused: paused,
    showClosing: step.kind === "closing",
    ending,
  };
}
