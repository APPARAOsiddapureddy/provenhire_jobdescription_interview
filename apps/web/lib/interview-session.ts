"use client";

/**
 * Owns the entire custom live-voice transport for one interview: mic capture,
 * a direct Deepgram STT WebSocket, the AI floor-state machine + barge-in
 * gating, and the coordination WebSocket to our own agent-api (which runs
 * the turn orchestrator and tells us what to speak next). No LiveKit
 * involved — see the Live Voice Pipeline Replacement plan.
 *
 * Deliberately owns the ONE `getUserMedia` stream and exposes `micTrack` so
 * the proctoring hooks (useMicNoiseGuard / useMicEnergy) can tap the same
 * track instead of each capturing their own.
 *
 * Explicitly out of scope: the reference architecture's camera/vision-fusion
 * turn-yield signal. Proctoring's camera analysis stays fully independent of
 * floor-state and barge-in decisions — don't wire it in here.
 */

import { isLikelyAiEcho } from "./interview-session/echo";

export type FloorState = "IDLE" | "AI_SPEAKING" | "AI_THINKING" | "USER_SPEAKING";

export type InterviewSessionErrorKind =
  | "mic_permission_denied"
  | "deepgram_connect_failed"
  | "coordination_connect_failed"
  | "tts_failed"
  | "unexpected";

export interface InterviewSessionError {
  kind: InterviewSessionErrorKind;
  message: string;
  cause?: unknown;
}

export interface InterviewSessionOptions {
  sessionId: string;
  /** Base WS origin for the agent-api, e.g. "wss://agent.example.com" (no
   * trailing slash, no path) — the browser connects to it directly since a
   * long-lived WS can't be proxied through Vercel. Sourced server-side from
   * AGENT_API_URL and passed down as a prop, same pattern as the old
   * LiveKit token/url props. */
  agentWsBaseUrl: string;
  onFloorChange?: (state: FloorState) => void;
  /** Candidate's own live STT text — interim (isFinal=false) or committed
   * (isFinal=true) for the current in-progress utterance. */
  onTranscript?: (text: string, isFinal: boolean) => void;
  /** Fired with the EXACT text the AI is about to speak, every time a new
   * "speak" message arrives. This is what the model actually says — which
   * is very often a paraphrase of the planned question, or an improvised
   * follow-up not in the plan at all — so the room should show THIS as the
   * current question, not a separately-polled plan snapshot. Polling
   * GET /api/session/{id} for questionText caused a real, confusing bug:
   * the display and the live audio would silently diverge as soon as the
   * model paraphrased or asked a follow-up. */
  onQuestionText?: (text: string) => void;
  onError?: (error: InterviewSessionError) => void;
  /** Fired once the server has ended the interview and the goodbye line (if
   * any) has finished playing. */
  onEnded?: () => void;
  /** The browser blocked autoplay of the AI's TTS audio (no prior user
   * gesture on this page). Call unlockAudio() from a click handler to
   * retry. */
  onAudioBlocked?: () => void;
}

/** Distinguishes "couldn't get a token" from "token was fine, WS wouldn't
 * open" purely for the error message text — see connectDeepgram(). */
class DeepgramTokenError extends Error {}

const DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen";
const CONNECT_TIMEOUT_MS = 8000;
const UTTERANCE_SAFETY_TIMEOUT_MS = 30_000;
const PARTIAL_MIN_GROWTH_CHARS = 12;
const PARTIAL_THROTTLE_MS = 350;
const SAMPLE_RATE = 16_000;

interface DeepgramAlternative {
  transcript: string;
}
interface DeepgramMessage {
  type: string;
  is_final?: boolean;
  channel?: { alternatives?: DeepgramAlternative[] };
}

type CoordinationMessage =
  | { type: "speak"; text: string }
  | { type: "error"; message: string }
  | { type: "session_conflict"; message: string }
  | { type: string; [key: string]: unknown };

/** WS close code the server sends after a graceful end (the end_interview
 * tool call, or SessionGuard's own wrap-up) — see api/live.py. Any other
 * close is either a rejection the server already explained via a message
 * (error / session_conflict, handled as it arrives — see
 * terminalMessageHandled below) or a genuine unexpected drop. */
const NORMAL_CLOSE_CODE = 1000;

function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms);
    promise.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (e) => {
        clearTimeout(timer);
        reject(e);
      },
    );
  });
}

function openWebSocket(url: string, protocols?: string | string[]): Promise<WebSocket> {
  return new Promise((resolve, reject) => {
    const ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url);
    const cleanup = () => {
      ws.removeEventListener("open", onOpen);
      ws.removeEventListener("error", onError);
    };
    const onOpen = () => {
      cleanup();
      resolve(ws);
    };
    const onError = () => {
      cleanup();
      reject(new Error(`WebSocket failed to open: ${url}`));
    };
    ws.addEventListener("open", onOpen);
    ws.addEventListener("error", onError);
  });
}

export class InterviewSession {
  private readonly sessionId: string;
  private readonly agentWsBaseUrl: string;
  private readonly onFloorChange?: (state: FloorState) => void;
  private readonly onTranscript?: (text: string, isFinal: boolean) => void;
  private readonly onQuestionText?: (text: string) => void;
  private readonly onError?: (error: InterviewSessionError) => void;
  private readonly onEnded?: () => void;
  private readonly onAudioBlocked?: () => void;

  private floor: FloorState = "IDLE";
  private closed = false;
  /** Set once an "error" or "session_conflict" coordination message has
   * already been surfaced via onError — the close event that follows it
   * must not ALSO fire onEnded() (which would silently redirect to the
   * report page over what the candidate just saw as a real error). */
  private terminalMessageHandled = false;

  private stream: MediaStream | null = null;
  private audioCtx: AudioContext | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private deepgramWs: WebSocket | null = null;
  private coordWs: WebSocket | null = null;
  private pendingSpeak: Promise<void> | null = null;

  private finalizedText = "";
  private lastSentPartial = "";
  private lastPartialSentAt = 0;
  private speechStartedSeen = false;
  private utteranceSafetyTimer: ReturnType<typeof setTimeout> | null = null;

  private lastAiText = "";
  private aiSpeechEndedAt: number | null = null;
  private ttsAbortController: AbortController | null = null;
  private currentAudio: HTMLAudioElement | null = null;
  private pendingAudio: HTMLAudioElement | null = null;

  constructor(options: InterviewSessionOptions) {
    this.sessionId = options.sessionId;
    this.agentWsBaseUrl = options.agentWsBaseUrl;
    this.onFloorChange = options.onFloorChange;
    this.onTranscript = options.onTranscript;
    this.onQuestionText = options.onQuestionText;
    this.onError = options.onError;
    this.onEnded = options.onEnded;
    this.onAudioBlocked = options.onAudioBlocked;
  }

  get floorState(): FloorState {
    return this.floor;
  }

  /** The single captured mic track, shared with the proctoring hooks. */
  get micTrack(): MediaStreamTrack | null {
    return this.stream?.getAudioTracks()[0] ?? null;
  }

  async connect(): Promise<void> {
    // Mic-permission wait is intentionally NOT part of the 8s race below —
    // how long a user takes to click "Allow" has nothing to do with
    // connection health, and racing it would produce false timeouts.
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
    } catch (err) {
      this.emitError("mic_permission_denied", "Microphone access was denied or unavailable.", err);
      throw err;
    }

    try {
      await withTimeout(this.connectTransports(), CONNECT_TIMEOUT_MS, "Connection setup timed out.");
    } catch (err) {
      this.close();
      throw err;
    }

    this.setFloor("IDLE");
  }

  /** Send typed text to the interviewer as if spoken — used by the room's
   * "repeat question" / "need a moment" buttons, which have no dedicated
   * server endpoint of their own; the orchestrator just treats this like any
   * other candidate utterance. Interrupts AI speech first if it's talking,
   * matching real barge-in behavior. */
  sendText(text: string): void {
    if (this.closed) return;
    const trimmed = text.trim();
    if (!trimmed) return;

    if (this.floor === "AI_SPEAKING" || this.floor === "AI_THINKING") {
      this.stopAiSpeech();
    }
    // Discard any in-progress candidate STT utterance so it can't get
    // double-committed once real speech resumes after this.
    this.finalizedText = "";
    this.lastSentPartial = "";
    this.clearUtteranceSafetyTimer();

    this.setFloor("AI_THINKING");
    if (this.coordWs?.readyState === WebSocket.OPEN) {
      this.coordWs.send(JSON.stringify({ type: "utterance_end", text: trimmed }));
    }
  }

  /** Retry a TTS playback that autoplay policy blocked — call from a click
   * handler (the modal's "Enable audio" button) so the retry itself
   * qualifies as a user gesture. */
  async unlockAudio(): Promise<void> {
    const audio = this.pendingAudio;
    if (!audio) return;
    try {
      await audio.play();
      this.pendingAudio = null;
    } catch {
      // Still blocked — caller can retry again on another gesture.
    }
  }

  private async connectTransports(): Promise<void> {
    try {
      await this.connectDeepgram();
    } catch (err) {
      const message =
        err instanceof DeepgramTokenError
          ? "Could not obtain a voice session token."
          : "Could not connect to the speech-to-text service.";
      this.emitError("deepgram_connect_failed", message, err);
      throw err;
    }

    const coordUrl = `${this.agentWsBaseUrl.replace(/\/$/, "")}/api/live/session/${encodeURIComponent(this.sessionId)}`;
    try {
      this.coordWs = await openWebSocket(coordUrl);
    } catch (err) {
      this.emitError("coordination_connect_failed", "Could not connect to the interview session.", err);
      throw err;
    }
    this.coordWs.addEventListener("message", (ev) => {
      this.pendingSpeak = this.handleCoordinationMessage(ev);
    });
    this.coordWs.addEventListener("close", (ev) => {
      if (this.closed) return;
      this.closed = true;
      void (async () => {
        // Let the goodbye line finish playing before signalling "ended" —
        // the server closes the socket right after sending it, which would
        // otherwise race the still-in-flight TTS fetch/playback.
        if (this.pendingSpeak) {
          try {
            await this.pendingSpeak;
          } catch {
            // already surfaced via onError inside speak()
          }
        }
        if (this.terminalMessageHandled) {
          // Already surfaced via onError when the error/session_conflict
          // message arrived, just above — don't also redirect to the
          // report page as if the interview ended normally.
          return;
        }
        if (ev.code === NORMAL_CLOSE_CODE) {
          this.onEnded?.();
        } else {
          // The server closed without a graceful end_interview handshake
          // and without an explanatory message first — a genuine drop
          // (network blip, server restart), not a completed interview.
          this.emitError(
            "unexpected",
            "The interview connection was lost unexpectedly.",
          );
        }
      })();
    });

    this.audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
    await this.audioCtx.audioWorklet.addModule("/worklets/pcm-processor.js");
    const source = this.audioCtx.createMediaStreamSource(this.stream as MediaStream);
    this.workletNode = new AudioWorkletNode(this.audioCtx, "pcm-processor");
    this.workletNode.port.onmessage = (ev: MessageEvent<ArrayBuffer>) => {
      if (this.deepgramWs?.readyState === WebSocket.OPEN) {
        this.deepgramWs.send(ev.data);
      }
    };
    source.connect(this.workletNode);
  }

  /** Mint a fresh token and open the Deepgram WS. Throws on failure — never
   * emits an error itself, since the SAME failure means something different
   * to each caller: fatal on the very first connect, just "try again" during
   * reconnectDeepgram()'s retry budget. Callers decide. */
  private async connectDeepgram(): Promise<void> {
    let token: string;
    try {
      const res = await fetch("/api/live/deepgram-token", { method: "POST" });
      if (!res.ok) throw new DeepgramTokenError(`token endpoint returned ${res.status}`);
      const json = (await res.json()) as { access_token: string };
      token = json.access_token;
    } catch (err) {
      throw err instanceof DeepgramTokenError ? err : new DeepgramTokenError(String(err));
    }

    // Sec-WebSocket-Protocol auth, NOT a query param: empirically verified
    // against the real Deepgram API (Aug 2026) — ?access_token=<grant JWT>
    // was rejected with a hard 401 "Invalid credentials" every time (not an
    // expiry issue; confirmed with tokens used within ~1s of minting).
    // Sec-WebSocket-Protocol: ["bearer", <JWT>] connects successfully; the
    // "token" subprotocol label (used for raw API keys) also 401s for a
    // grant JWT — it specifically needs the "bearer" label.
    const dgUrl = new URL(DEEPGRAM_WS_URL);
    dgUrl.searchParams.set("model", "nova-3");
    dgUrl.searchParams.set("language", "en"); // English-first v1 — see the rework plan
    dgUrl.searchParams.set("encoding", "linear16");
    dgUrl.searchParams.set("sample_rate", String(SAMPLE_RATE));
    dgUrl.searchParams.set("channels", "1");
    dgUrl.searchParams.set("interim_results", "true");
    dgUrl.searchParams.set("vad_events", "true");
    dgUrl.searchParams.set("endpointing", "1500");
    dgUrl.searchParams.set("utterance_end_ms", "2800");

    const ws = await openWebSocket(dgUrl.toString(), ["bearer", token]);
    ws.addEventListener("message", (ev) => this.handleDeepgramMessage(ev));
    ws.addEventListener("close", () => this.onDeepgramClosed(ws));
    this.deepgramWs = ws;
  }

  /** A real-world Deepgram WS can drop mid-interview for reasons that have
   * nothing to do with the interview itself — a network blip, a backgrounded
   * browser tab throttling timers, a transient upstream hiccup. Treating
   * that as instantly fatal (the original behavior) ends a perfectly
   * recoverable interview over a hiccup in ONE of three connections. Retry
   * a few times with backoff before actually giving up. */
  private deepgramReconnectAttempt = 0;
  private static readonly MAX_DEEPGRAM_RECONNECT_ATTEMPTS = 4;

  private onDeepgramClosed(closedWs: WebSocket): void {
    // Ignore close events from a socket we've already superseded (a stale
    // listener from a previous connectDeepgram() call) or our own teardown.
    if (this.closed || this.deepgramWs !== closedWs) return;
    void this.reconnectDeepgram();
  }

  private async reconnectDeepgram(): Promise<void> {
    if (this.closed) return;
    this.deepgramReconnectAttempt += 1;
    if (this.deepgramReconnectAttempt > InterviewSession.MAX_DEEPGRAM_RECONNECT_ATTEMPTS) {
      this.emitError(
        "deepgram_connect_failed",
        "Speech-to-text connection closed unexpectedly.",
      );
      return;
    }
    const backoffMs = 500 * 2 ** (this.deepgramReconnectAttempt - 1); // 0.5s, 1s, 2s, 4s
    await new Promise((resolve) => setTimeout(resolve, backoffMs));
    if (this.closed) return;
    try {
      await this.connectDeepgram();
      this.deepgramReconnectAttempt = 0; // back to healthy — reset the budget
    } catch {
      // connectDeepgram() never emits on its own — silently try again up to
      // the attempt budget above, which is what actually surfaces a fatal
      // error once retries are exhausted.
      void this.reconnectDeepgram();
    }
  }

  // --- Deepgram STT event handling ---------------------------------------

  private handleDeepgramMessage(ev: MessageEvent<string>): void {
    let msg: DeepgramMessage;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }

    if (msg.type === "SpeechStarted") {
      this.speechStartedSeen = true;
      return;
    }
    if (msg.type === "UtteranceEnd") {
      this.commitUtterance();
      return;
    }
    if (msg.type !== "Results") return;

    const text = (msg.channel?.alternatives?.[0]?.transcript ?? "").trim();

    // Step 1: hard gates.
    if (this.closed || !text) return;

    // Step 2: 3-gate barge-in check — only matters while the AI holds the
    // floor. All three must pass for a fragment to count as a real barge-in:
    // real voice activity (VAD), a non-trivial fragment, and not an echo of
    // what the AI itself is saying.
    if (this.floor === "AI_SPEAKING" || this.floor === "AI_THINKING") {
      const msSinceAiSpoke = this.floor === "AI_SPEAKING" ? 0 : (this.aiSpeechEndedAt !== null ? Date.now() - this.aiSpeechEndedAt : Infinity);
      const gates = {
        voiceDetected: this.speechStartedSeen,
        substantial: text.length >= 3,
        notEcho: !isLikelyAiEcho(text, this.lastAiText, msSinceAiSpoke),
      };
      if (!(gates.voiceDetected && gates.substantial && gates.notEcho)) return;
      this.takeFloor("USER_SPEAKING");
    } else if (this.floor === "IDLE") {
      this.setFloor("USER_SPEAKING");
    }

    const isFinal = Boolean(msg.is_final);

    // Step 3: final-fragment accumulation.
    if (isFinal) {
      this.finalizedText = this.finalizedText ? `${this.finalizedText} ${text}` : text;
      this.onTranscript?.(this.finalizedText, true);
      this.sendPartial(this.finalizedText, true);
      return;
    }

    // Step 4: interim fragments are display-only — never committed.
    const preview = this.finalizedText ? `${this.finalizedText} ${text}` : text;
    this.onTranscript?.(preview, false);
    this.sendPartial(preview, false);

    // Step 5: no vision-fusion step, by design — camera/proctoring signals
    // never factor into floor-taking or barge-in decisions here.
  }

  private sendPartial(text: string, isFinal: boolean): void {
    if (!this.coordWs || this.coordWs.readyState !== WebSocket.OPEN) return;
    const now = Date.now();
    if (!isFinal) {
      const grown = text.length - this.lastSentPartial.length;
      if (!(grown >= PARTIAL_MIN_GROWTH_CHARS && now - this.lastPartialSentAt >= PARTIAL_THROTTLE_MS)) {
        return;
      }
    }
    this.lastPartialSentAt = now;
    this.lastSentPartial = text;
    this.coordWs.send(JSON.stringify({ type: "partial_transcript", text }));
  }

  private commitUtterance(): void {
    const text = this.finalizedText.trim();
    this.finalizedText = "";
    this.lastSentPartial = "";
    this.speechStartedSeen = false;
    this.clearUtteranceSafetyTimer();

    if (!text) {
      if (this.floor === "USER_SPEAKING") this.setFloor("IDLE");
      return;
    }

    this.setFloor("AI_THINKING");
    if (this.coordWs?.readyState === WebSocket.OPEN) {
      this.coordWs.send(JSON.stringify({ type: "utterance_end", text }));
    }
  }

  private armUtteranceSafetyTimer(): void {
    this.clearUtteranceSafetyTimer();
    // The only two triggers that ever commit an utterance: Deepgram's own
    // UtteranceEnd event, and this watchdog if that event never arrives.
    this.utteranceSafetyTimer = setTimeout(() => this.commitUtterance(), UTTERANCE_SAFETY_TIMEOUT_MS);
  }

  private clearUtteranceSafetyTimer(): void {
    if (this.utteranceSafetyTimer) {
      clearTimeout(this.utteranceSafetyTimer);
      this.utteranceSafetyTimer = null;
    }
  }

  // --- Floor-state machine -------------------------------------------------

  private setFloor(next: FloorState): void {
    if (this.floor === next) return;
    const prev = this.floor;
    this.floor = next;

    if (next === "AI_SPEAKING" || next === "AI_THINKING") {
      // AI takes the floor — drop any buffered mic audio so stale candidate
      // audio queued in the worklet doesn't get sent once the floor flips.
      this.workletNode?.port.postMessage("clear");
      this.speechStartedSeen = false;
    }
    if (next === "USER_SPEAKING" && prev !== "USER_SPEAKING") {
      this.armUtteranceSafetyTimer();
    }
    if (prev === "USER_SPEAKING" && next !== "USER_SPEAKING") {
      this.clearUtteranceSafetyTimer();
    }

    this.onFloorChange?.(next);
  }

  private takeFloor(next: FloorState): void {
    if (this.floor === "AI_SPEAKING" || this.floor === "AI_THINKING") {
      this.stopAiSpeech();
    }
    this.setFloor(next);
  }

  private stopAiSpeech(): void {
    this.ttsAbortController?.abort();
    this.ttsAbortController = null;
    if (this.currentAudio) {
      this.currentAudio.pause();
      this.currentAudio.src = "";
      this.currentAudio = null;
    }
    if (this.pendingAudio) {
      this.pendingAudio.pause();
      this.pendingAudio.src = "";
      this.pendingAudio = null;
    }
  }

  // --- Coordination WS + TTS playback --------------------------------------

  private async handleCoordinationMessage(ev: MessageEvent<string>): Promise<void> {
    let msg: CoordinationMessage;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.type === "error") {
      this.terminalMessageHandled = true;
      this.emitError("unexpected", String(msg.message ?? "The interview session reported an error."));
      return;
    }
    if (msg.type === "session_conflict") {
      this.terminalMessageHandled = true;
      this.emitError(
        "unexpected",
        String(
          msg.message ??
            "This interview is already open in another tab or window.",
        ),
      );
      return;
    }
    if (msg.type !== "speak") return;

    const text = String((msg as { text?: string }).text ?? "").trim();
    if (!text) return;
    await this.speak(text);
  }

  private async speak(text: string): Promise<void> {
    this.lastAiText = text;
    this.onQuestionText?.(text);
    this.setFloor("AI_THINKING");

    const controller = new AbortController();
    this.ttsAbortController = controller;

    let audioBlob: Blob;
    try {
      const res = await fetch("/api/live/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`tts endpoint returned ${res.status}`);
      audioBlob = await res.blob();
    } catch (err) {
      if (controller.signal.aborted) return; // barged-in mid-fetch — not a real error
      this.emitError("tts_failed", "Could not generate speech audio.", err);
      if (this.floor === "AI_THINKING") this.setFloor("IDLE");
      return;
    }

    if (controller.signal.aborted || this.closed) return;

    const url = URL.createObjectURL(audioBlob);
    const audio = new Audio(url);
    this.currentAudio = audio;
    this.setFloor("AI_SPEAKING");

    await new Promise<void>((resolve) => {
      const cleanup = () => {
        URL.revokeObjectURL(url);
        resolve();
      };
      audio.addEventListener("ended", cleanup, { once: true });
      audio.addEventListener("error", cleanup, { once: true });
      audio.play().catch((err) => {
        if (err instanceof DOMException && err.name === "NotAllowedError") {
          // Autoplay blocked — stash it and wait for unlockAudio() to retry
          // from a real user gesture; "ended"/"error" above still resolve
          // this promise once that succeeds (or the barge-in path aborts it).
          this.pendingAudio = audio;
          this.onAudioBlocked?.();
          return;
        }
        cleanup();
      });
    });

    // Guard against a barge-in having already started a newer speak() call
    // while this one was still awaiting playback — don't let a stale call
    // clobber floor state set by the newer one.
    if (this.currentAudio === audio) {
      this.currentAudio = null;
      this.ttsAbortController = null;
      this.aiSpeechEndedAt = Date.now();
      if (this.floor === "AI_SPEAKING") this.setFloor("IDLE");
    }
  }

  // --- Teardown -------------------------------------------------------------

  close(): void {
    if (this.closed) return;
    this.closed = true;

    this.clearUtteranceSafetyTimer();
    this.stopAiSpeech();

    if (this.workletNode) {
      this.workletNode.port.onmessage = null;
      this.workletNode.disconnect();
      this.workletNode = null;
    }
    if (this.stream) {
      for (const track of this.stream.getTracks()) track.stop();
      this.stream = null;
    }
    if (this.audioCtx) {
      this.audioCtx.close().catch(() => {});
      this.audioCtx = null;
    }
    if (this.deepgramWs) {
      this.deepgramWs.onmessage = null;
      this.deepgramWs.onclose = null;
      if (this.deepgramWs.readyState === WebSocket.OPEN) this.deepgramWs.close();
      this.deepgramWs = null;
    }
    if (this.coordWs) {
      this.coordWs.onmessage = null;
      this.coordWs.onclose = null;
      if (this.coordWs.readyState === WebSocket.OPEN) this.coordWs.close();
      this.coordWs = null;
    }
    this.floor = "IDLE";
  }

  private emitError(kind: InterviewSessionErrorKind, message: string, cause?: unknown): void {
    this.onError?.({ kind, message, cause });
  }
}
