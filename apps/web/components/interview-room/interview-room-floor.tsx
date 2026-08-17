"use client";

/**
 * <InterviewRoomFloor> — the flagship live-interview screen.
 *
 * Pure presentational component: a phase state machine drives everything via
 * props, every user action is a callback — nothing here talks to LiveKit,
 * the agent API, or any network directly. The page that mounts this owns all
 * of that (see app/interview-room/[session_id]/page.tsx), which keeps this
 * file testable/reusable and keeps the (considerable) visual-motion system
 * isolated from data concerns.
 *
 * Layout mirrors the sibling Antigravity Interview product's control-room
 * floor: AI presence (left) — the interview stage (center) — candidate
 * corner (right), with the current Q/A always the visual center and the AI's
 * aura as its only "body language" (no fake human avatar).
 */

import * as React from "react";
import { cn } from "@/lib/cn";
import { AIOrb, type OrbState } from "./ai-orb";
import { PHButton, PHSectionLabel } from "@/components/design-system";

export type RoomPhase =
  | "ready"
  | "asking"
  | "listening"
  | "reviewing"
  | "closing";

const PHASE_CONFIG: Record<
  RoomPhase,
  { aura: string; label: string; orb: OrbState }
> = {
  ready: { aura: "#7AA7FF", label: "Ready when you are", orb: "idle" },
  asking: { aura: "#FF9A3D", label: "Asking question", orb: "speaking" },
  listening: {
    aura: "#1FD5F9",
    label: "Listening to candidate's answer",
    orb: "listening",
  },
  reviewing: {
    aura: "#FFBE59",
    label: "Reviewing candidate's answer",
    orb: "thinking",
  },
  closing: { aura: "#6CFF9E", label: "Closing interview", orb: "idle" },
};

const MURAL_BY_PHASE: Record<RoomPhase, [string, string, string]> = {
  ready: ["#7AA7FF", "#1FD5F9", "#6CFF9E"],
  asking: ["#FF9A3D", "#7AA7FF", "#FFBE59"],
  listening: ["#1FD5F9", "#7AA7FF", "#6CFF9E"],
  reviewing: ["#FFBE59", "#FF9A3D", "#7AA7FF"],
  closing: ["#6CFF9E", "#1FD5F9", "#7AA7FF"],
};

export interface TurnHistoryEntry {
  id: string;
  question: string;
  answer: string | null;
}

export interface InterviewRoomFloorProps {
  /* Identity / header */
  candidateName: string;
  candidateInitials: string;
  role: string;
  company?: string | null;
  experienceLabel?: string | null;
  stageLabel: string;

  /* Phase state machine */
  phase: RoomPhase;
  floorOwner: "ai" | "candidate" | null;

  /* Progress */
  questionIndex: number; // 0-based
  totalQuestions: number;

  /* Current question / transcript */
  questionText: string;
  transcriptText: string;
  transcriptOpen: boolean;
  onToggleTranscript: () => void;

  /* Mic / audio */
  micEnergy: number; // 0..1
  micReady: boolean;

  /* Camera */
  cameraOn: boolean;
  cameraStream: MediaStream | null;
  onToggleCamera: () => void;

  /* Candidate corner / history */
  historyOpen: boolean;
  onToggleHistory: () => void;
  fullTranscriptMode: boolean;
  onToggleFullTranscript: () => void;
  turnHistory: TurnHistoryEntry[];

  /* Controls */
  pauseCount: number; // remaining "need a moment" uses
  pauseMax: number;
  onRequestPause: () => void;
  onRepeatQuestion: () => void;
  onEndInterview: () => void;
  ending: boolean;

  /* Modals */
  showCountdown: boolean;
  countdownValue: number | null;
  showConfirmEnd: boolean;
  onConfirmEnd: () => void;
  onCancelEnd: () => void;
  showPaused: boolean;
  onContinueFromPause: () => void;
  showClosing: boolean;

  /* Misc */
  privateSession?: boolean;
  className?: string;
}

/* ────────────────────────────────────────────────────────────────────
   Background — layered radial gradients keyed to the phase's mural colors,
   a diagonal caustic-drift stripe layer, vignette + dot-grain, all via
   color-mix() against the room's own base gradient.
   ──────────────────────────────────────────────────────────────────── */

function RoomBackground({ phase }: { phase: RoomPhase }) {
  const [a, b, c] = MURAL_BY_PHASE[phase];
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 -z-10">
      <div
        className="absolute inset-0 transition-[background] duration-700"
        style={{
          background: `
            radial-gradient(46% 38% at 18% 8%, color-mix(in oklch, ${a} 30%, transparent), transparent 70%),
            radial-gradient(40% 34% at 86% 10%, color-mix(in oklch, ${b} 22%, transparent), transparent 70%),
            radial-gradient(50% 44% at 50% 100%, color-mix(in oklch, ${c} 16%, transparent), transparent 72%),
            linear-gradient(180deg, #071014 0%, #020304 82%)
          `,
        }}
      />
      {/* Caustic drift stripes. */}
      <div
        className="ph-anim-caustic absolute inset-0"
        style={{
          backgroundImage: `repeating-linear-gradient(38deg, color-mix(in oklch, ${a} 10%, transparent) 0px, transparent 2px, transparent 60px)`,
          backgroundSize: "180% 180%",
          maskImage: "radial-gradient(70% 60% at 50% 40%, black, transparent)",
          WebkitMaskImage:
            "radial-gradient(70% 60% at 50% 40%, black, transparent)",
        }}
      />
      {/* Dot-grain texture, fading by 72% height. */}
      <div
        className="absolute inset-0"
        style={{
          backgroundImage:
            "radial-gradient(oklch(1 0 0 / 0.05) 1px, transparent 1px)",
          backgroundSize: "30px 30px",
          maskImage: "linear-gradient(180deg, black, transparent 72%)",
          WebkitMaskImage: "linear-gradient(180deg, black, transparent 72%)",
        }}
      />
      {/* Vignette. */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(120% 100% at 50% 50%, transparent 55%, oklch(0 0 0 / 0.55) 100%)",
        }}
      />
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────
   Topbar
   ──────────────────────────────────────────────────────────────────── */

function Topbar({
  candidateName,
  role,
  phase,
  experienceLabel,
  micReady,
  privateSession,
}: Pick<
  InterviewRoomFloorProps,
  "candidateName" | "role" | "phase" | "experienceLabel" | "micReady"
> & { privateSession?: boolean }) {
  const cfg = PHASE_CONFIG[phase];
  return (
    <div
      className="rounded-room flex items-center justify-between gap-4 border border-white/10 px-5 py-3.5 backdrop-blur-[22px] backdrop-saturate-[1.12]"
      style={{ background: "oklch(0.14 0.014 265 / 0.55)" }}
    >
      <div className="flex min-w-0 items-center gap-3">
        <span
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/15"
          style={{ background: `color-mix(in oklch, ${cfg.aura} 18%, transparent)` }}
        >
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: cfg.aura, boxShadow: `0 0 8px ${cfg.aura}` }}
          />
        </span>
        <div className="min-w-0">
          <p className="ph-kicker truncate text-white/50">
            ProvenHire Interview
          </p>
          <p className="truncate text-[14px] font-medium text-white">
            {candidateName} · {role}
          </p>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <TopbarPill dotColor={cfg.aura} label={cfg.label} />
        {experienceLabel && <TopbarPill label={experienceLabel} />}
        <TopbarPill
          dotColor={micReady ? "#6CFF9E" : "#FF9A3D"}
          label={micReady ? "Mic ready" : "Mic muted"}
        />
        {privateSession !== false && <TopbarPill label="Private session" />}
      </div>
    </div>
  );
}

function TopbarPill({ label, dotColor }: { label: string; dotColor?: string }) {
  return (
    <span className="hidden items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.1em] text-white/70 sm:inline-flex">
      {dotColor && (
        <span
          className="h-1.5 w-1.5 rounded-full"
          style={{ background: dotColor }}
        />
      )}
      {label}
    </span>
  );
}

/* ────────────────────────────────────────────────────────────────────
   Left panel — AI presence
   ──────────────────────────────────────────────────────────────────── */

function LeftPanel({
  phase,
  micEnergy,
}: Pick<InterviewRoomFloorProps, "phase" | "micEnergy">) {
  const cfg = PHASE_CONFIG[phase];
  const energyPct = 8 + micEnergy * 72;
  return (
    <div className="flex h-full flex-col gap-4 rounded-room border border-white/10 bg-white/[0.02] p-5">
      <PHSectionLabel className="!text-white/45">
        AI interviewer
      </PHSectionLabel>
      <p className="text-[15px] font-medium text-white">{cfg.label}</p>

      <div className="flex min-h-[300px] flex-1 items-center justify-center">
        <AIOrb state={cfg.orb} micEnergy={micEnergy} size={220} />
      </div>

      <p className="text-center text-[12px] leading-relaxed text-white/45">
        {phase === "asking" && "Reading the next question aloud…"}
        {phase === "listening" && "Taking in the candidate's answer."}
        {phase === "reviewing" && "Weighing the answer against the JD."}
        {phase === "ready" && "Waiting for the candidate to begin."}
        {phase === "closing" && "Wrapping up — thank you for your time."}
      </p>

      {/* Voice-energy meter. */}
      <div className="h-1 w-full overflow-hidden rounded-full bg-white/10">
        <div
          className="h-full rounded-full transition-[width] duration-150"
          style={{
            width: `${energyPct}%`,
            background: `linear-gradient(90deg, ${cfg.aura}, ${MURAL_BY_PHASE[phase][1]})`,
          }}
        />
      </div>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────
   Center panel — the interview stage
   ──────────────────────────────────────────────────────────────────── */

function ProgressDots({
  questionIndex,
  totalQuestions,
  aura,
}: {
  questionIndex: number;
  totalQuestions: number;
  aura: string;
}) {
  const count = Math.min(Math.max(totalQuestions, 1), 15);
  return (
    <div className="flex items-center justify-center gap-1.5">
      {Array.from({ length: count }, (_, i) => {
        const filled = i < questionIndex;
        const active = i === questionIndex;
        return (
          <span
            key={i}
            className="h-1.5 w-5 rounded-full transition-colors duration-300"
            style={{
              background: filled || active ? aura : "oklch(1 0 0 / 0.12)",
              boxShadow: active ? `0 0 10px ${aura}` : undefined,
            }}
          />
        );
      })}
    </div>
  );
}

function FloorRail({
  floorOwner,
  questionIndex,
  totalQuestions,
}: {
  floorOwner: "ai" | "candidate" | null;
  questionIndex: number;
  totalQuestions: number;
}) {
  return (
    <div className="flex items-center justify-center gap-3 text-[11px] font-medium">
      <span
        className={cn(
          "flex items-center gap-1.5 rounded-full px-2.5 py-1",
          floorOwner === "ai" ? "text-white" : "text-white/35",
        )}
      >
        {floorOwner === "ai" && (
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: "#FF9A3D", boxShadow: "0 0 6px #FF9A3D" }}
          />
        )}
        AI corner
      </span>
      <span className="ph-kicker text-white/40">
        Turn {Math.min(questionIndex + 1, totalQuestions)} of {totalQuestions}
      </span>
      <span
        className={cn(
          "flex items-center gap-1.5 rounded-full px-2.5 py-1",
          floorOwner === "candidate" ? "text-white" : "text-white/35",
        )}
      >
        {floorOwner === "candidate" && (
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: "#1FD5F9", boxShadow: "0 0 6px #1FD5F9" }}
          />
        )}
        Candidate corner
      </span>
    </div>
  );
}

function ActivityPill({ phase }: { phase: RoomPhase }) {
  const cfg = PHASE_CONFIG[phase];
  const text: Record<RoomPhase, string> = {
    ready: "Interview room is ready",
    asking: "Interviewer asking",
    listening: "Candidate speaking",
    reviewing: "Interviewer thinking",
    closing: "Closing interview",
  };
  return (
    <div className="flex justify-center">
      <span
        className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.05] px-3.5 py-1.5 text-[12px] text-white/80"
      >
        <span
          className="h-1.5 w-1.5 rounded-full"
          style={{ background: cfg.aura, boxShadow: `0 0 6px ${cfg.aura}` }}
        />
        {text[phase]}
      </span>
    </div>
  );
}

/** 4 density tiers by question length — a pragmatic stand-in for a live
 * binary-search-to-fit measurement: long questions get the smaller clamp
 * range and a taller card, short ones get the largest type. */
function questionDensity(text: string): {
  minHeight: string;
  fontSize: string;
} {
  const len = text.length;
  if (len <= 60) return { minHeight: "clamp(178px, 20vh, 210px)", fontSize: "clamp(34px, 3.25vw, 54px)" };
  if (len <= 120) return { minHeight: "clamp(210px, 24vh, 250px)", fontSize: "clamp(30px, 2.8vw, 46px)" };
  if (len <= 200) return { minHeight: "clamp(250px, 28vh, 292px)", fontSize: "clamp(26px, 2.3vw, 38px)" };
  return { minHeight: "clamp(292px, 32vh, 332px)", fontSize: "clamp(23px, 2vw, 32px)" };
}

function QuestionCard({ questionText }: { questionText: string }) {
  const { minHeight, fontSize } = questionDensity(questionText);
  return (
    <div className="flex flex-col gap-2">
      <PHSectionLabel className="!text-white/40">
        Interviewer&rsquo;s question
      </PHSectionLabel>
      <div
        className="flex items-center rounded-room bg-black px-8 py-8"
        style={{ minHeight }}
      >
        <p
          className="font-semibold text-white"
          style={{
            fontSize,
            fontWeight: 780,
            lineHeight: 1.04,
            textShadow: "0 2px 24px oklch(0 0 0 / 0.5)",
          }}
        >
          {questionText}
        </p>
      </div>
    </div>
  );
}

function TranscriptPanel({
  transcriptText,
  transcriptOpen,
  onToggleTranscript,
  listening,
}: {
  transcriptText: string;
  transcriptOpen: boolean;
  onToggleTranscript: () => void;
  listening: boolean;
}) {
  return (
    <div
      className="ph-scrollbar overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03] transition-[height] duration-300"
      style={{ height: transcriptOpen ? "min(132px, 16vh)" : "70px" }}
    >
      <div className="flex items-center justify-between px-4 py-3">
        <PHSectionLabel className="!text-white/40">
          Live transcript
        </PHSectionLabel>
        <button
          type="button"
          onClick={onToggleTranscript}
          className="font-mono text-[10px] font-semibold uppercase tracking-[0.1em] text-white/50 hover:text-white"
        >
          {transcriptOpen ? "Hide" : "Show"}
        </button>
      </div>
      {transcriptOpen && (
        <div className="px-4 pb-3 text-[13px] leading-relaxed text-white/75">
          {transcriptText || (
            <span className="text-white/30">Waiting for the candidate…</span>
          )}
          {listening && (
            <span className="ph-anim-caret ml-0.5 inline-block h-[14px] w-[2px] translate-y-[2px] bg-white/70" />
          )}
        </div>
      )}
    </div>
  );
}

function ActionBar({
  pauseCount,
  pauseMax,
  onRequestPause,
  onRepeatQuestion,
  onEndInterview,
  ending,
}: Pick<
  InterviewRoomFloorProps,
  | "pauseCount"
  | "pauseMax"
  | "onRequestPause"
  | "onRepeatQuestion"
  | "onEndInterview"
  | "ending"
>) {
  return (
    <div className="flex flex-wrap items-center gap-2.5 border-t border-white/10 pt-4">
      <PHButton variant="primary" onClick={onRepeatQuestion} disabled={ending}>
        Repeat question
      </PHButton>
      <PHButton
        variant="secondary"
        onClick={onRequestPause}
        disabled={pauseCount <= 0 || ending}
      >
        Need a moment ({pauseCount})
      </PHButton>
      <PHButton
        variant="danger"
        className="ml-auto"
        onClick={onEndInterview}
        disabled={ending}
      >
        {ending ? "Ending…" : "End interview"}
      </PHButton>
      <span className="ph-kicker w-full text-white/25 sm:w-auto">
        {pauseMax - pauseCount}/{pauseMax} pauses used
      </span>
    </div>
  );
}

function CenterPanel(props: InterviewRoomFloorProps) {
  const cfg = PHASE_CONFIG[props.phase];
  return (
    <div className="flex h-full min-w-0 flex-col gap-5 rounded-room border border-white/10 bg-white/[0.015] p-6">
      <ProgressDots
        questionIndex={props.questionIndex}
        totalQuestions={props.totalQuestions}
        aura={cfg.aura}
      />
      <FloorRail
        floorOwner={props.floorOwner}
        questionIndex={props.questionIndex}
        totalQuestions={props.totalQuestions}
      />
      <ActivityPill phase={props.phase} />

      <div className="flex flex-1 flex-col justify-center gap-5">
        <QuestionCard questionText={props.questionText} />
        <TranscriptPanel
          transcriptText={props.transcriptText}
          transcriptOpen={props.transcriptOpen}
          onToggleTranscript={props.onToggleTranscript}
          listening={props.phase === "listening"}
        />
      </div>

      <ActionBar
        pauseCount={props.pauseCount}
        pauseMax={props.pauseMax}
        onRequestPause={props.onRequestPause}
        onRepeatQuestion={props.onRepeatQuestion}
        onEndInterview={props.onEndInterview}
        ending={props.ending}
      />
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────
   Right panel — candidate corner
   ──────────────────────────────────────────────────────────────────── */

function CameraPreview({
  cameraOn,
  cameraStream,
  candidateInitials,
}: {
  cameraOn: boolean;
  cameraStream: MediaStream | null;
  candidateInitials: string;
}) {
  const videoRef = React.useRef<HTMLVideoElement | null>(null);
  React.useEffect(() => {
    if (videoRef.current) videoRef.current.srcObject = cameraStream;
  }, [cameraStream]);

  return (
    <div className="relative aspect-[4/3] w-full overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03]">
      {cameraOn && cameraStream ? (
        <video
          ref={videoRef}
          autoPlay
          muted
          playsInline
          className="h-full w-full object-cover"
          style={{ transform: "scaleX(-1)" }}
        />
      ) : (
        <div className="flex h-full w-full items-center justify-center">
          <span className="flex h-14 w-14 items-center justify-center rounded-full bg-white/10 font-mono text-[16px] font-semibold text-white/70">
            {candidateInitials}
          </span>
        </div>
      )}
      {!cameraOn && (
        <span className="absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full border border-white/10 bg-black/60 px-2.5 py-1 font-mono text-[9px] uppercase tracking-[0.1em] text-white/60">
          Preview hidden
        </span>
      )}
    </div>
  );
}

function RightPanel({
  historyOpen,
  onToggleHistory,
  cameraOn,
  cameraStream,
  onToggleCamera,
  candidateInitials,
  turnHistory,
  fullTranscriptMode,
  onToggleFullTranscript,
}: Pick<
  InterviewRoomFloorProps,
  | "historyOpen"
  | "onToggleHistory"
  | "cameraOn"
  | "cameraStream"
  | "onToggleCamera"
  | "candidateInitials"
  | "turnHistory"
  | "fullTranscriptMode"
  | "onToggleFullTranscript"
>) {
  if (!historyOpen) {
    return (
      <button
        type="button"
        onClick={onToggleHistory}
        className="flex h-full w-[72px] flex-col items-center justify-center gap-3 rounded-room border border-white/10 bg-white/[0.02] transition-colors hover:bg-white/[0.05]"
      >
        <span
          className="font-mono text-[10px] font-semibold uppercase tracking-[0.2em] text-white/50"
          style={{ writingMode: "vertical-rl" }}
        >
          Candidate corner
        </span>
      </button>
    );
  }

  return (
    <div className="flex h-full flex-col gap-4 rounded-room border border-white/10 bg-white/[0.02] p-4">
      <div className="flex items-center justify-between">
        <PHSectionLabel className="!text-white/45">
          Candidate corner
        </PHSectionLabel>
        <button
          type="button"
          onClick={onToggleHistory}
          className="font-mono text-[10px] uppercase tracking-[0.1em] text-white/40 hover:text-white"
        >
          Collapse
        </button>
      </div>

      <div className="flex items-center justify-between">
        <CameraPreview
          cameraOn={cameraOn}
          cameraStream={cameraStream}
          candidateInitials={candidateInitials}
        />
      </div>
      <button
        type="button"
        onClick={onToggleCamera}
        className="self-start font-mono text-[10px] uppercase tracking-[0.1em] text-white/50 hover:text-white"
      >
        {cameraOn ? "Turn camera off" : "Turn camera on"}
      </button>

      <div className="mt-1 flex items-center justify-between">
        <p className="text-[12px] font-medium text-white/70">
          Question log
        </p>
        <button
          type="button"
          onClick={onToggleFullTranscript}
          className="font-mono text-[10px] uppercase tracking-[0.1em] text-white/40 hover:text-white"
        >
          {fullTranscriptMode ? "Hide" : "Show"} full transcript
        </button>
      </div>

      <div className="ph-scrollbar flex-1 space-y-2 overflow-y-auto pr-1">
        {turnHistory.length === 0 && (
          <p className="text-[12px] text-white/30">
            Questions will appear here as the interview goes.
          </p>
        )}
        {turnHistory.map((turn, i) => (
          <div
            key={turn.id}
            className="rounded-xl border border-white/10 bg-white/[0.03] p-3"
          >
            {i === turnHistory.length - 1 && (
              <p className="ph-kicker mb-1 !text-[var(--ph-blue)]">
                Latest question
              </p>
            )}
            <p className="text-[12.5px] leading-snug text-white/85">
              {turn.question}
            </p>
            {fullTranscriptMode && turn.answer && (
              <p className="mt-1.5 text-[12px] leading-snug text-white/50">
                {turn.answer}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────
   Modal overlays
   ──────────────────────────────────────────────────────────────────── */

// Exported so InterviewRoomLive can reuse the exact same modal chrome for
// the real-voice-only concerns that don't belong in this presentational
// component's own prop contract (e.g. the browser audio-unlock gate).
export function ModalScaffold({
  titleId,
  children,
}: {
  titleId: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex items-center justify-center p-6"
      style={{ backdropFilter: "blur(24px)", background: "oklch(0.02 0 0 / 0.55)" }}
    >
      <div className="ph-card flex max-w-md flex-col items-center gap-4 rounded-room px-10 py-10 text-center">
        {children}
      </div>
    </div>
  );
}

function CountdownModal({ value }: { value: number | null }) {
  return (
    <ModalScaffold titleId="ph-countdown-title">
      <p id="ph-countdown-title" className="ph-kicker !text-white/50">
        Get ready
      </p>
      <p
        className="font-mono font-bold text-[var(--ph-blue)]"
        style={{ fontSize: "clamp(64px, 12vw, 140px)" }}
      >
        {value ?? ""}
      </p>
    </ModalScaffold>
  );
}

function ConfirmEndModal({
  onConfirm,
  onCancel,
}: {
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <ModalScaffold titleId="ph-confirm-end-title">
      <p id="ph-confirm-end-title" className="text-[19px] font-semibold text-white">
        Are you sure?
      </p>
      <p className="text-[13px] leading-relaxed text-white/60">
        Ending now closes the interview and takes you to your report.
      </p>
      <div className="mt-2 flex gap-3">
        <PHButton variant="secondary" onClick={onCancel} autoFocus>
          Stay
        </PHButton>
        <PHButton variant="danger" onClick={onConfirm}>
          End interview
        </PHButton>
      </div>
    </ModalScaffold>
  );
}

function PausedModal({
  remaining,
  onContinue,
}: {
  remaining: number;
  onContinue: () => void;
}) {
  return (
    <ModalScaffold titleId="ph-paused-title">
      <p id="ph-paused-title" className="text-[19px] font-semibold text-white">
        Take your time
      </p>
      <p className="text-[13px] leading-relaxed text-white/60">
        {remaining} pause{remaining === 1 ? "" : "s"} remaining.
      </p>
      <PHButton variant="primary" onClick={onContinue} autoFocus>
        Continue interview
      </PHButton>
    </ModalScaffold>
  );
}

function ClosingModal() {
  return (
    <ModalScaffold titleId="ph-closing-title">
      <p id="ph-closing-title" className="text-[22px] font-semibold text-white">
        Interview complete
      </p>
      <p className="text-[13px] leading-relaxed text-white/60">
        Your report is being prepared…
      </p>
      <span
        className="h-6 w-6 animate-spin rounded-full border-2 border-[var(--ph-blue)] border-t-transparent"
        aria-hidden
      />
    </ModalScaffold>
  );
}

/* ────────────────────────────────────────────────────────────────────
   Orchestrator
   ──────────────────────────────────────────────────────────────────── */

export function InterviewRoomFloor(props: InterviewRoomFloorProps) {
  const anyModalOpen =
    props.showCountdown ||
    props.showConfirmEnd ||
    props.showPaused ||
    props.showClosing;

  return (
    <div
      className={cn(
        "ph-shell relative mx-auto grid h-screen w-full",
        "grid-rows-[auto_minmax(0,1fr)] gap-4 p-4",
        anyModalOpen && "[filter:saturate(0.74)_brightness(0.74)]",
        props.className,
      )}
      style={{ width: "min(1480px, 100vw - 32px)" }}
    >
      <RoomBackground phase={props.phase} />

      <Topbar
        candidateName={props.candidateName}
        role={props.role}
        phase={props.phase}
        experienceLabel={props.experienceLabel}
        micReady={props.micReady}
        privateSession={props.privateSession}
      />

      <div
        className="ph-room-grid min-h-0"
        data-history-open={props.historyOpen}
      >
        <div className="min-h-0">
          <LeftPanel phase={props.phase} micEnergy={props.micEnergy} />
        </div>

        <div className="min-h-0">
          <CenterPanel {...props} />
        </div>

        <div className="min-h-0">
          <RightPanel
            historyOpen={props.historyOpen}
            onToggleHistory={props.onToggleHistory}
            cameraOn={props.cameraOn}
            cameraStream={props.cameraStream}
            onToggleCamera={props.onToggleCamera}
            candidateInitials={props.candidateInitials}
            turnHistory={props.turnHistory}
            fullTranscriptMode={props.fullTranscriptMode}
            onToggleFullTranscript={props.onToggleFullTranscript}
          />
        </div>
      </div>

      {props.showCountdown && <CountdownModal value={props.countdownValue} />}
      {props.showConfirmEnd && (
        <ConfirmEndModal
          onConfirm={props.onConfirmEnd}
          onCancel={props.onCancelEnd}
        />
      )}
      {props.showPaused && (
        <PausedModal
          remaining={props.pauseMax - props.pauseCount}
          onContinue={props.onContinueFromPause}
        />
      )}
      {props.showClosing && <ClosingModal />}
    </div>
  );
}
