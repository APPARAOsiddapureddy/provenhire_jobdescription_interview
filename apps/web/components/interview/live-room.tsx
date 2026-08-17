"use client";

/**
 * <LiveRoom> — the WP-2 live interview screen orchestrator.
 *
 * Two strictly-separated paths share the SAME calm, centered layout:
 *
 *  • LIVE  (token && url): renders `<LiveKitRoom connect audio video={false}>`
 *    with `<RoomAudioRenderer/>` + `<StartAudio/>` and the hook-driven
 *    `<LiveSession/>`. Every LiveKit hook (useVoiceAssistant, useTranscriptions,
 *    useLocalParticipant, useConnectionState) lives ONLY inside
 *    that provider — `<LiveSession/>` is never rendered outside it.
 *
 *  • PREVIEW (no token): renders the identical layout from static sample data
 *    via `<PreviewSession/>`, which calls ZERO LiveKit hooks. This is the
 *    offline path the build runs with no env keys — the page renders fully.
 *
 * The room subtree is additionally gated behind a mounted flag so the
 * browser-only LiveKit client never runs during SSR/hydration.
 *
 * Failure UX (nothing fails silently):
 *  • Mic failure (permission denied / no device / device busy) → an assertive
 *    banner explains how to fix it; the room stays connected so the candidate
 *    can still hear the interviewer and type answers via the text fallback.
 *  • Connection lost (room error, server-side disconnect) → an honest
 *    "Connection lost" state with a Rejoin button that remounts the room.
 *  • Reconnecting (transient network blips) → a polite live-region notice.
 *  • A server-closed room (interview ended) routes to the report instead.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useConnectionState,
  useLocalParticipant,
  useStartAudio,
  useTranscriptions,
} from "@livekit/components-react";
import {
  ConnectionState,
  DisconnectReason,
  MediaDeviceFailure,
} from "livekit-client";
import "@livekit/components-styles";

import type { Persona } from "@/lib/personas";
import { cn } from "@/lib/cn";
import { t } from "@/lib/i18n";
import { useMessages } from "@/lib/i18n/client";
import { useIntegrityMonitor, useScreenRecording } from "@/lib/integrity";
import { VoiceStage, StagePreview } from "@/components/interview/voice-stage";
import {
  TranscriptPanel,
  type Turn,
} from "@/components/interview/transcript-panel";
import { ControlBar } from "@/components/interview/control-bar";
import { SessionTimer } from "@/components/interview/session-timer";
import { TextFallback } from "@/components/interview/text-fallback";

// The text-stream topic livekit-agents' RoomIO registers its chat handler on
// (TOPIC_CHAT in the Python SDK). Typed answers MUST go here to reach the agent.
const AGENT_CHAT_TOPIC = "lk.chat";

const SAMPLE_TURNS: Turn[] = [
  {
    id: "s1",
    role: "interviewer",
    text: "Thanks for joining. To start, walk me through a project you're proud of and the part you personally owned.",
  },
  {
    id: "s2",
    role: "candidate",
    text: "Sure. I led the migration of our checkout service off a monolith — I owned the rollout plan and the fallback path.",
  },
  {
    id: "s3",
    role: "interviewer",
    text: "What was the riskiest part of that rollout, and how did you de-risk it?",
  },
];

export interface LiveRoomProps {
  sessionId: string;
  persona: Persona;
  /** Null when LiveKit is unconfigured → preview (not-connected) mode. */
  token: string | null;
  url: string | null;
  /** Screen recording needs R2 to store the recording — computed server-side. */
  r2Configured: boolean;
}

/**
 * Human, fixable guidance for a microphone failure. Permission denial is the
 * common case — the browser blocked getUserMedia — so we say exactly where to
 * flip the switch instead of a generic "something went wrong".
 */
function describeMicFailure(failure?: MediaDeviceFailure): string {
  switch (failure) {
    case MediaDeviceFailure.PermissionDenied:
      return "Microphone access is blocked. Click the lock or mic icon in your browser's address bar, allow the microphone, then unmute.";
    case MediaDeviceFailure.NotFound:
      return "No microphone was found. Connect one (or pick a different input in your system settings), then unmute.";
    case MediaDeviceFailure.DeviceInUse:
      return "Your microphone is in use by another app. Close that app, then unmute.";
    default:
      return "We couldn't access your microphone. Check your browser's microphone permission, then unmute.";
  }
}

/**
 * Accessible failure banner for the scaffold's notice slot. `role="alert"` +
 * assertive aria-live so screen readers announce it the moment it appears.
 */
function ErrorNotice({
  title,
  message,
  action,
}: {
  title: string;
  message: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      role="alert"
      aria-live="assertive"
      className={cn(
        "mx-auto mt-6 w-full max-w-xl rounded-card border border-accent/40",
        "bg-paper/80 px-4 py-3 text-center backdrop-blur-sm",
      )}
    >
      <p className="text-[13px] font-medium text-ink">{title}</p>
      <p className="mt-1 text-[13px] leading-relaxed text-ink-soft">
        {message}
      </p>
      {action ? <div className="mt-3 flex justify-center">{action}</div> : null}
    </div>
  );
}

/**
 * Full-screen, impossible-to-miss modal prompt — a click-to-continue gate
 * for anything that needs a fresh user gesture to unblock (browser autoplay
 * policy, entering fullscreen; both require a real click, not a programmatic
 * call). Generalized from the audio-unlock overlay below so Integrity
 * Controls' "Fullscreen Required" can reuse the identical template instead
 * of duplicating it.
 *
 * THE BUG the audio case originally fixed: the previous UI used LiveKit's
 * <StartAudio>, a small text button sitting quietly among the other
 * controls. When a browser blocked autoplay, a candidate had no audio cue
 * that anything was wrong — the transcript kept filling in with the
 * interviewer's questions (proving "the AI is working"), but nothing was
 * ever heard, and no visual signal pointed at the one small button that
 * would fix it. A full-screen blocking overlay makes the fix impossible to
 * miss — the same reasoning applies to any other gesture-gated requirement.
 */
function BlockingPrompt({
  titleId,
  title,
  message,
  buttonLabel,
  onContinue,
}: {
  titleId: string;
  title: string;
  message: string;
  buttonLabel: string;
  onContinue: () => void;
}) {
  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 backdrop-blur-sm"
    >
      <div className="mx-4 flex max-w-sm flex-col items-center gap-4 rounded-card border border-line bg-paper px-8 py-8 text-center shadow-xl">
        <p id={titleId} className="font-serif text-[19px] text-ink">
          {title}
        </p>
        <p className="text-[13px] leading-relaxed text-ink-soft">{message}</p>
        <button
          type="button"
          onClick={onContinue}
          autoFocus
          className="mt-1 rounded-full bg-ink px-6 py-3 text-[14px] font-medium text-white transition-colors hover:bg-ink-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-paper"
        >
          {buttonLabel}
        </button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Shared scaffold — both paths render this exact frame.               */
/* ------------------------------------------------------------------ */

function Scaffold({
  persona,
  stage,
  transcript,
  timer,
  controls,
  textFallback,
  notice,
}: {
  persona: Persona;
  stage: React.ReactNode;
  transcript: React.ReactNode;
  timer: React.ReactNode;
  controls: React.ReactNode;
  textFallback: React.ReactNode;
  notice?: React.ReactNode;
}) {
  return (
    <main className="relative min-h-screen overflow-hidden bg-paper">
      {/* Calm backdrop wash behind the frosted panels. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(110% 80% at 50% -10%, rgba(67,56,202,0.06), transparent 60%)",
        }}
      />

      <div className="relative mx-auto flex min-h-screen w-full max-w-5xl flex-col px-6 py-8">
        {/* Top row: who + the clock. */}
        <div className="flex items-center justify-between">
          <div className="flex flex-col">
            <span className="font-mono text-[10px] tracking-[0.16em] text-faint">
              PROVEN HIRE · LIVE
            </span>
            <span className="font-serif text-[17px] text-ink">
              {persona.name}
            </span>
          </div>
          {timer}
        </div>

        {/* Centerpiece: avatar + transcript, calm two-column on wide. */}
        <div className="mt-6 grid flex-1 items-center gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
          <div className="mx-auto w-full max-w-sm lg:max-w-md">{stage}</div>
          <div className="flex h-full max-h-[60vh] min-h-[280px] flex-col lg:max-h-[68vh]">
            {transcript}
          </div>
        </div>

        {notice}

        {/* Controls + accessible text fallback. */}
        <div className="mt-6 flex flex-col items-center gap-4">
          {controls}
          <div className="w-full max-w-xl">{textFallback}</div>
        </div>

        {/* Always shown, independent of Integrity Controls settings — see
            IntegrityForm's footer note ("maintains recruiter trust"). */}
        <p className="mt-4 text-center text-[11px] text-faint">
          {t(useMessages(), "common.integrityTrustLine")}
        </p>
      </div>
    </main>
  );
}

/* ------------------------------------------------------------------ */
/* LIVE session — descendant of <LiveKitRoom>; owns all LiveKit hooks. */
/* ------------------------------------------------------------------ */

function LiveSession({
  persona,
  sessionId,
  micError,
  onMicError,
  r2Configured,
}: {
  persona: Persona;
  sessionId: string;
  /** Mic-failure banner text — owned by <LiveRoom>, which also catches room-level capture failures. */
  micError: string | null;
  onMicError: (message: string | null) => void;
  r2Configured: boolean;
}) {
  const router = useRouter();
  const connectionState = useConnectionState();
  const connected = connectionState === ConnectionState.Connected;
  const reconnecting =
    connectionState === ConnectionState.Reconnecting ||
    connectionState === ConnectionState.SignalReconnecting;

  const { localParticipant, isMicrophoneEnabled } = useLocalParticipant();
  const transcriptions = useTranscriptions();
  const { mergedProps: startAudioProps, canPlayAudio } = useStartAudio({
    props: {},
  });

  const [ending, setEnding] = React.useState(false);
  const endingRef = React.useRef(false);

  // A publishing mic proves capture works again — clear any stale failure banner.
  React.useEffect(() => {
    if (isMicrophoneEnabled) onMicError(null);
  }, [isMicrophoneEnabled, onMicError]);

  // Typed answers bypass STT, so nothing in `transcriptions` ever carries
  // them — without a local echo the typed answer is invisible and the
  // interviewer appears to reply to nothing.
  const [typedTurns, setTypedTurns] = React.useState<
    { id: string; text: string; at: number }[]
  >([]);

  // Map streaming transcriptions + typed answers → turns, in send/receive
  // order. "You" when the segment belongs to the local participant, otherwise
  // the interviewer (agent / avatar worker). STT finalizes speech in short
  // segments, so consecutive segments from the same speaker are merged into
  // one turn — one paragraph per speaking turn, not a new "You" label per
  // sentence fragment.
  const turns: Turn[] = React.useMemo(() => {
    const localIdentity = localParticipant.identity;
    const entries: (Turn & { at: number })[] = [];
    for (const t of transcriptions) {
      const text = t.text.trim();
      if (!text) continue;
      entries.push({
        at: t.streamInfo.timestamp ?? 0,
        id: t.streamInfo.id,
        role:
          t.participantInfo.identity === localIdentity
            ? ("candidate" as const)
            : ("interviewer" as const),
        text,
      });
    }
    for (const t of typedTurns) {
      entries.push({ at: t.at, id: t.id, role: "candidate", text: t.text });
    }
    entries.sort((a, b) => a.at - b.at);
    const merged: Turn[] = [];
    for (const entry of entries) {
      const last = merged[merged.length - 1];
      if (last?.role === entry.role) {
        last.text = `${last.text} ${entry.text}`;
      } else {
        merged.push({ id: entry.id, role: entry.role, text: entry.text });
      }
    }
    return merged;
  }, [transcriptions, typedTurns, localParticipant.identity]);

  async function toggleMute() {
    try {
      await localParticipant.setMicrophoneEnabled(!isMicrophoneEnabled);
      onMicError(null);
    } catch (error) {
      // getUserMedia rejection: permission denied / no device / device busy.
      onMicError(describeMicFailure(MediaDeviceFailure.getFailure(error)));
    }
  }

  async function endInterview() {
    // Re-entrancy guard: a ref (not the `ending` state, which updates
    // asynchronously) so two guard hooks hitting their 3rd strike in the
    // same tick — or a strike landing right as the End button is clicked —
    // can't both fall through and double-run the leave sequence.
    if (endingRef.current) return;
    endingRef.current = true;
    setEnding(true);
    try {
      await localParticipant.setMicrophoneEnabled(false);
    } catch {
      // best-effort; proceed to leave regardless
    }
    // Best-effort, bounded: give the screen recording (if any) a chance to
    // upload before navigating away, but never let a slow upload strand the
    // candidate on this screen — this app never blocks on a background task
    // indefinitely (same philosophy as every other integrity check here).
    await Promise.race([
      stopAndUpload(),
      new Promise((resolve) => setTimeout(resolve, 8000)),
    ]);
    // The room disconnect happens as <LiveKitRoom> unmounts on navigation.
    router.push(`/report/${encodeURIComponent(sessionId)}`);
  }

  // useIntegrityMonitor needs a STABLE onAutoEnd identity. `endInterview`
  // itself is a fresh function every render (it closes over per-render
  // state/props), so it's stashed in a ref and read through a callback whose
  // identity never changes. Without this, a new `() => void endInterview()`
  // closure each render churned reportViolation's identity inside
  // useIntegrityMonitor, which is a dependency of EVERY guard hook
  // (fullscreen/tab/devtools/copy-paste/mic-noise/camera) — tearing all six
  // down and rebuilding them on every re-render (e.g. every transcript
  // segment). Concretely this meant the camera-AI hook could restart its
  // MediaPipe/TensorFlow model load before it ever finished, and the
  // mic-noise guard's consecutive-loud-frame counter reset before it could
  // ever reach its threshold — both detectors effectively never firing.
  const endInterviewRef = React.useRef<() => void>(() => {});
  endInterviewRef.current = () => void endInterview();
  const onAutoEnd = React.useCallback(() => endInterviewRef.current(), []);

  // Integrity Controls: fetches settings once, wires the browser-signal
  // guards (fullscreen/tab-switch/devtools/copy-paste/mic-noise), and calls
  // endInterview() itself once a rule class hits 3 strikes AND "3-strike
  // auto-end" is STRICT — same auto-end path the End button uses.
  const {
    settings: integritySettings,
    banner,
    needsFullscreen,
    requestFullscreen,
    cameraStatus,
  } = useIntegrityMonitor(onAutoEnd);
  const cameraRequired = integritySettings?.camera_required !== "off";
  const cameraUnavailable =
    cameraRequired &&
    (cameraStatus === "denied" || cameraStatus === "unsupported");

  // Screen recording is independent of the "AI Behavior Analysis" master
  // switch (it's plain capture, not a detector) and only ever activates
  // when R2 is actually configured — there's nowhere else a recording this
  // large could reasonably go (unlike a CV, there's no small-file fallback).
  const {
    needsConsent: needsRecordingConsent,
    grantConsent: grantRecordingConsent,
    stopAndUpload,
  } = useScreenRecording(
    integritySettings?.screen_recording_enabled,
    sessionId,
    r2Configured,
  );

  function sendText(text: string) {
    // Optimistic local echo — the agent does not stream typed input back.
    setTypedTurns((prev) => [
      ...prev,
      { id: `typed-${Date.now()}-${prev.length}`, text, at: Date.now() },
    ]);
    // Text streams on "lk.chat" are what the LiveKit agent's RoomIO consumes
    // (register_text_stream_handler(TOPIC_CHAT)). A raw data-channel publish on
    // a custom topic is never delivered to the agent — the typed answer would
    // silently vanish.
    void localParticipant
      .sendText(text, { topic: AGENT_CHAT_TOPIC })
      .catch(() => {
        // no-op if the room isn't ready
      });
  }

  return (
    <>
      {/* Blocking overlay — not a small button candidates can miss. Shown the
          moment the browser has refused autoplay; disappears the instant the
          click registers (canPlayAudio flips true), no reload needed. */}
      {/* At most one blocking prompt renders at a time. All three share the
          same `fixed inset-0 z-50` stacking context, so if more than one of
          these were ever true simultaneously (very plausible at session
          start: audio unlock, fullscreen, and recording consent can all be
          pending together), rendering them as independent siblings left
          whichever mounted first hidden underneath the latest one — the
          candidate stuck behind an invisible gate with no visible way
          forward. Chaining them into one priority order guarantees exactly
          one is ever shown, and each is re-checked every render, so the next
          one (if still needed) takes over the instant the current one
          resolves. Priority: hearing the interviewer first, then
          fullscreen, then recording consent. */}
      {!canPlayAudio ? (
        <BlockingPrompt
          titleId="audio-unlock-title"
          title="Enable interview audio"
          message="Your browser blocked the interviewer's voice from playing automatically. Click below so you can hear them — this only takes one click."
          buttonLabel="Enable audio & continue"
          onContinue={() => startAudioProps.onClick()}
        />
      ) : needsFullscreen ? (
        <BlockingPrompt
          titleId="fullscreen-required-title"
          title="Fullscreen required"
          message="This assessment requires fullscreen mode. Click below to continue."
          buttonLabel="Enter fullscreen & continue"
          onContinue={requestFullscreen}
        />
      ) : needsRecordingConsent ? (
        <BlockingPrompt
          titleId="recording-consent-title"
          title="Screen recording required"
          message="This assessment records your screen for review. Click below, then choose this tab or your full screen in the browser's share picker."
          buttonLabel="Start screen recording"
          onContinue={grantRecordingConsent}
        />
      ) : null}
      <Scaffold
        persona={persona}
        stage={<VoiceStage persona={persona} />}
        transcript={<TranscriptPanel turns={turns} live className="h-full" />}
        timer={<SessionTimer running={connected} />}
        controls={
          <ControlBar
            micEnabled={isMicrophoneEnabled}
            onToggleMute={() => void toggleMute()}
            onEnd={() => void endInterview()}
            ending={ending}
          />
        }
        textFallback={<TextFallback onSend={sendText} />}
        notice={
          banner ? (
            <ErrorNotice
              title={banner.level === "strict" ? "Integrity warning" : "Notice"}
              message={banner.message}
            />
          ) : micError ? (
            <ErrorNotice
              title="Microphone unavailable"
              message={`${micError} Until then, you can type your answers below.`}
            />
          ) : cameraUnavailable ? (
            <ErrorNotice
              title="Camera required"
              message="This assessment requires your camera. Check your browser's camera permission, then reload."
            />
          ) : reconnecting ? (
            <div
              role="status"
              aria-live="polite"
              className="mx-auto mt-6 max-w-xl rounded-card border border-line bg-paper/70 px-4 py-3 text-center text-[13px] text-muted backdrop-blur-sm"
            >
              Connection unstable — reconnecting…
            </div>
          ) : null
        }
      />
    </>
  );
}

/* ------------------------------------------------------------------ */
/* PREVIEW session — zero LiveKit hooks; static sample data offline.    */
/* ------------------------------------------------------------------ */

function PreviewSession({ persona }: { persona: Persona }) {
  return (
    <Scaffold
      persona={persona}
      stage={<StagePreview persona={persona} />}
      transcript={<TranscriptPanel turns={SAMPLE_TURNS} className="h-full" />}
      timer={<SessionTimer running={false} />}
      controls={
        <ControlBar
          micEnabled
          onToggleMute={() => {}}
          onEnd={() => {}}
          disabled
        />
      }
      textFallback={<TextFallback onSend={() => {}} disabled />}
      notice={
        <div
          className={cn(
            "mx-auto mt-6 max-w-xl rounded-card border border-line",
            "bg-paper/70 px-4 py-3 text-center text-[13px] text-muted backdrop-blur-sm",
          )}
          role="status"
        >
          Preview mode — connect LiveKit (set{" "}
          <code className="font-mono text-[12px] text-ink-soft">LIVEKIT_*</code>{" "}
          in <code className="font-mono text-[12px] text-ink-soft">.env</code>)
          to go live.
        </div>
      }
    />
  );
}

/* ------------------------------------------------------------------ */
/* Connecting shell — pre-mount of the genuine live path (no env notice).*/
/* ------------------------------------------------------------------ */

function ConnectingShell({ persona }: { persona: Persona }) {
  return (
    <Scaffold
      persona={persona}
      stage={<StagePreview persona={persona} />}
      transcript={<TranscriptPanel turns={[]} className="h-full" />}
      timer={<SessionTimer running={false} />}
      controls={
        <ControlBar
          micEnabled
          onToggleMute={() => {}}
          onEnd={() => {}}
          disabled
        />
      }
      textFallback={<TextFallback onSend={() => {}} disabled />}
      notice={
        <div
          className="mx-auto mt-6 max-w-xl rounded-card border border-line bg-paper/70 px-4 py-3 text-center text-[13px] text-muted backdrop-blur-sm"
          role="status"
        >
          Connecting…
        </div>
      }
    />
  );
}

/* ------------------------------------------------------------------ */
/* Connection-lost shell — honest failure state with a rejoin action.   */
/* ------------------------------------------------------------------ */

function ConnectionLostShell({
  persona,
  message,
  onRejoin,
}: {
  persona: Persona;
  message: string;
  onRejoin: () => void;
}) {
  return (
    <Scaffold
      persona={persona}
      stage={<StagePreview persona={persona} />}
      transcript={<TranscriptPanel turns={[]} className="h-full" />}
      timer={<SessionTimer running={false} />}
      controls={
        <ControlBar
          micEnabled
          onToggleMute={() => {}}
          onEnd={() => {}}
          disabled
        />
      }
      textFallback={<TextFallback onSend={() => {}} disabled />}
      notice={
        <ErrorNotice
          title="Connection lost"
          message={message}
          action={
            <button
              type="button"
              onClick={onRejoin}
              className="rounded-full border border-accent bg-accent-soft px-4 py-2 text-[13px] font-medium text-accent transition-colors hover:bg-accent hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-paper"
            >
              Rejoin interview
            </button>
          }
        />
      }
    />
  );
}

/* ------------------------------------------------------------------ */
/* Orchestrator.                                                        */
/* ------------------------------------------------------------------ */

export function LiveRoom({
  sessionId,
  persona,
  token,
  url,
  r2Configured,
}: LiveRoomProps) {
  const router = useRouter();

  // Guard the browser-only LiveKit client from SSR / first hydration.
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);

  // Failure UX state. `micError` keeps the room mounted (the candidate can
  // still hear the interviewer and type answers); `connectionLost` unmounts
  // the room and offers a rejoin. `joinAttempt` keys <LiveKitRoom> so each
  // rejoin is a clean reconnect.
  const [micError, setMicError] = React.useState<string | null>(null);
  const [connectionLost, setConnectionLost] = React.useState<string | null>(
    null,
  );
  const [joinAttempt, setJoinAttempt] = React.useState(0);

  const handleError = React.useCallback((error: Error) => {
    // Device-capture failures surface here too (the room enables the mic via
    // the `audio` prop); classify them as mic problems, not connection loss.
    const failure = MediaDeviceFailure.getFailure(error);
    if (failure) {
      setMicError(describeMicFailure(failure));
      return;
    }
    setConnectionLost(
      "We couldn't reach the interview room. Check your network, then rejoin.",
    );
  }, []);

  const handleMediaDeviceFailure = React.useCallback(
    (failure?: MediaDeviceFailure) => {
      setMicError(describeMicFailure(failure));
    },
    [],
  );

  const handleDisconnected = React.useCallback(
    (reason?: DisconnectReason) => {
      // Intentional leave (End interview → navigation): not an error.
      if (reason === DisconnectReason.CLIENT_INITIATED) return;
      // The server closed the room — the interview is over; show the report.
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

  function rejoin() {
    setMicError(null);
    setConnectionLost(null);
    setJoinAttempt((n) => n + 1);
  }

  const canConnect = Boolean(token && url);

  // Offline / not-connected: the build + no-keys path. Renders fully.
  if (!canConnect) {
    return <PreviewSession persona={persona} />;
  }

  // Before mount (genuine live path), show a neutral connecting shell so SSR
  // markup matches and the browser-only LiveKit client spins up client-side —
  // without flashing the offline "set LIVEKIT_*" notice.
  if (!mounted) {
    return <ConnectingShell persona={persona} />;
  }

  // Honest failure state: never leave the candidate staring at an idle avatar.
  if (connectionLost) {
    return (
      <ConnectionLostShell
        persona={persona}
        message={connectionLost}
        onRejoin={rejoin}
      />
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
      onMediaDeviceFailure={handleMediaDeviceFailure}
      onDisconnected={handleDisconnected}
    >
      <RoomAudioRenderer />
      <LiveSession
        persona={persona}
        sessionId={sessionId}
        micError={micError}
        onMicError={setMicError}
        r2Configured={r2Configured}
      />
    </LiveKitRoom>
  );
}
