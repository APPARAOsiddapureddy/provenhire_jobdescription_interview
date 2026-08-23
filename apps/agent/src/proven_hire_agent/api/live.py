"""``/api/live`` — the custom (non-LiveKit) live-voice transport's backend seam.

Three pieces (Live Voice Pipeline Replacement, Phases 1 & 3):

- ``POST /api/live/deepgram-token`` — mints a short-lived, scoped Deepgram
  token for the browser's direct STT WebSocket connection, rather than
  handing out the raw ``DEEPGRAM_API_KEY``.
- ``POST /api/live/tts`` — generates TTS audio bytes (Cartesia REST) for one
  utterance; the browser fetches this per-utterance with its own
  ``AbortController`` for barge-in (see ``InterviewSession``).
- ``ws /api/live/session/{session_id}`` — the coordination WebSocket: the
  browser forwards its own Deepgram-committed utterances here, and this
  drives the ``live/orchestrator.py`` turn loop (real OpenAI tool-calling)
  and pushes back what to speak next.

``router`` (the two REST endpoints) is internal-secret gated like every
other write/compute route, proxied through the web app
(``apps/web/app/api/live/*/route.ts``) exactly like ``/api/coach/chat``.
``ws_router`` (the WebSocket) is deliberately NOT — a browser's native
WebSocket API cannot set custom headers, and Vercel's serverless functions
don't proxy long-lived WebSockets, so the browser connects to this route on
the agent-api DIRECTLY. It is capability-guarded by the session_id alone
(unguessable uuid4), the same posture ``GET /api/session/{id}`` already
uses for exactly this reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict

from ..core.config import Settings
from ..core.deps import Deps, build_deps
from ..core.logging import get_logger
from ..core.observability import capture_error
from ..core.rate_limit import SlidingWindowRateLimiter, client_ip
from ..core.session_status import TERMINAL_STATUSES
from ..live import orchestrator as orch
from ..live import protocol, resilience
from ..live.guard import SessionGuard, wrap_up_line
from ..live.orchestrator import CompleteFn, CompletionResult, LiveTurnSession, ToolCall, TurnResult
from ..live.persistence import flush_checkpoint, persist_and_score
from ..live.state import InterviewUserdata

log = get_logger(__name__)

router = APIRouter()
ws_router = APIRouter()

# Ingress rate limits (Phase 5, backend hardening plan) — genuinely new
# code, no prior precedent. Module-level singletons, matching every other
# process-wide state in this codebase (MemoryRepository etc.): a
# per-request instance would reset its window on every call and limit
# nothing. Scoped to the single-process assumption — see rate_limit.py.
# Thresholds are a reasoned starting point (generous enough that no
# legitimate candidate session should ever hit them), not tuned against
# real traffic yet.
_deepgram_token_limiter = SlidingWindowRateLimiter(max_events=10, window_sec=60.0)
_tts_limiter = SlidingWindowRateLimiter(max_events=40, window_sec=60.0)
# Keyed by session_id (not IP): each candidate turn is one real LLM call —
# 20/min is generous for genuine back-and-forth conversation (well under
# one every 3s) while still bounding a stuck/looping client from spamming
# real cost indefinitely within a single session.
_ws_turn_limiter = SlidingWindowRateLimiter(max_events=20, window_sec=60.0)
# A spoken answer transcribed via STT; a few minutes of continuous speech
# comfortably fits, same reasoning as the TTS/proctoring-events caps
# elsewhere in this file.
_MAX_UTTERANCE_LEN = 5_000

_DEEPGRAM_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"
# Deepgram's grant endpoint's own default/max TTL differs by plan; 60s is
# comfortably more than the ~8s the client allows itself to open the
# Deepgram WS (see InterviewSession's connect() timeout race) while staying
# short-lived. Re-verify against current Deepgram docs if grants start
# failing with a TTL-out-of-range error.
_TOKEN_TTL_SEC = 60


class DeepgramTokenResponse(BaseModel):
    """API-internal (both ends are this repo's own code) — not in
    shared_models, since this never needs TS<->Pydantic parity checking."""

    model_config = ConfigDict(extra="forbid")
    access_token: str
    expires_in: int


@router.post("/api/live/deepgram-token", response_model=DeepgramTokenResponse)
async def mint_deepgram_token(request: Request) -> DeepgramTokenResponse:
    settings = build_deps().settings
    if not settings.deepgram_api_key:
        raise HTTPException(status_code=503, detail="Deepgram is not configured")
    ip = client_ip(request)
    if not _deepgram_token_limiter.allow(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many token requests",
            headers={"Retry-After": str(int(_deepgram_token_limiter.retry_after(ip)) + 1)},
        )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _DEEPGRAM_GRANT_URL,
            headers={"Authorization": f"Token {settings.deepgram_api_key}"},
            json={"ttl_seconds": _TOKEN_TTL_SEC},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Deepgram token grant failed: {resp.status_code} {resp.text[:200]}",
        )
    data = resp.json()
    return DeepgramTokenResponse(
        access_token=data["access_token"], expires_in=data.get("expires_in", _TOKEN_TTL_SEC)
    )


# --- TTS -----------------------------------------------------------------

_CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"
_CARTESIA_VERSION = "2025-04-16"
_MAX_TTS_TEXT_LEN = 2_000  # one spoken turn, generous; never a whole transcript


class TtsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


@router.post("/api/live/tts")
async def synthesize_tts(req: TtsRequest, request: Request):
    from fastapi import Response

    settings = build_deps().settings
    if not settings.cartesia_api_key:
        raise HTTPException(status_code=503, detail="Cartesia TTS is not configured")
    if len(req.text) > _MAX_TTS_TEXT_LEN:
        raise HTTPException(status_code=413, detail="Text too long for one TTS call")
    ip = client_ip(request)
    if not _tts_limiter.allow(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many TTS requests",
            headers={"Retry-After": str(int(_tts_limiter.retry_after(ip)) + 1)},
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            _CARTESIA_TTS_URL,
            headers={
                "X-API-Key": settings.cartesia_api_key,
                "Cartesia-Version": _CARTESIA_VERSION,
                "Content-Type": "application/json",
            },
            json={
                "model_id": "sonic-2",
                "transcript": req.text,
                "voice": {"mode": "id", "id": settings.cartesia_voice_id},
                "output_format": {
                    "container": "mp3",
                    "bit_rate": 128000,
                    "sample_rate": 44100,
                },
                "language": "en",
            },
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Cartesia TTS failed: {resp.status_code} {resp.text[:200]}",
        )
    return Response(content=resp.content, media_type="audio/mpeg")


# --- coordination WebSocket -------------------------------------------------

# gpt-oss models generate internally using OpenAI's "Harmony" response format
# (channel-tagged: an "analysis" reasoning channel, a "final" answer channel).
# A known issue across MULTIPLE inference backends (vLLM, SGLang — not
# specific to one provider) is that the OpenAI-compatible endpoint doesn't
# always fully strip Harmony's own markup, leaking either full tag sequences
# (``<|channel|>final<|message|>``) or just the bare channel name glued
# directly onto the real reply (observed live: "finalSure, Alex..."). More
# likely under long system prompts — which this project's interview
# instructions are. Only relevant when live_llm_provider is a gpt-oss host
# (cerebras/groq/together); harmless no-op against OpenAI's own API.
# The "analysis" channel is the model's private reasoning/scratchpad — when
# it leaks it can be several sentences of plain prose (observed live, e.g.
# "We have the follow-up signal indicating we should move on... So we
# proceed to next turn awaiting answer.assistantfinalGot it. Could you..."),
# not just stray tags, so this can't be caught by matching tag syntax alone.
# Empirically the real reply consistently starts right after the LAST
# occurrence of a "final" (optionally "assistantfinal") marker immediately
# followed by an uppercase letter — whatever led up to it, tagged or not, is
# the leaked reasoning and must never reach TTS. Greedy .* naturally finds
# the RIGHTMOST such marker (regex backtracks from the end of the string).
_HARMONY_TAG_RE = re.compile(r"<\|[a-zA-Z_]+\|>")
_HARMONY_REASONING_PREFIX_RE = re.compile(r"^.*(?:assistant)?final(?=[A-Z])", re.DOTALL)


def _clean_harmony_leak(text: str | None) -> str | None:
    if not text:
        return text
    cleaned = _HARMONY_TAG_RE.sub("", text)
    cleaned = _HARMONY_REASONING_PREFIX_RE.sub("", cleaned)
    return cleaned.strip()


# Every OpenAI-API-compatible backend the live loop can use. Fixed failover
# priority order (Phase 4): whichever is primary (settings.live_llm_provider)
# is tried first; the others are tried in THIS order, skipping any without a
# configured key. OpenAI first among the fallbacks since it's the most
# reliable/well-provisioned option when it isn't already primary.
_LIVE_PROVIDERS: tuple[str, ...] = ("openai", "together", "cerebras", "groq")


def _live_provider_configured(settings: Settings, provider: str) -> bool:
    if provider == "cerebras":
        return bool(settings.cerebras_api_key)
    if provider == "groq":
        return bool(settings.groq_api_key)
    if provider == "together":
        return bool(settings.together_api_key)
    return bool(settings.openai_api_key)


def _make_provider_complete_fn(settings: Settings, provider: str) -> CompleteFn:
    """Real CompleteFn for ONE provider — "openai", "cerebras", "groq", or
    "together". All four are OpenAI-API-compatible (same SDK, same
    tool-calling shape, just a ``base_url``/key/model swap); the three
    alternates to OpenAI were wired in because account-activation issues
    (billing holds, signup fraud flags) turned out to be the real obstacle
    in practice, not model/API availability, and are measured materially
    faster for this specific tool-calling loop (~7-9s -> ~2-3s per turn).
    Raises ``RuntimeError`` if ``provider``'s key isn't configured.
    """
    from openai import AsyncOpenAI

    # gpt-oss's reasoning_effort trades reasoning depth for latency (and,
    # empirically, fewer malformed Harmony-format leaks — see
    # _clean_harmony_leak above) — "low" fits a real-time voice loop where
    # snappy turn-taking matters more than deep chain-of-thought. Only
    # meaningful for gpt-oss hosts; omitted for OpenAI's own API.
    extra_body: dict = {"reasoning_effort": "low"} if provider in {"cerebras", "groq", "together"} else {}
    if provider == "cerebras":
        if not settings.cerebras_api_key:
            raise RuntimeError("CEREBRAS_API_KEY is not configured; the live orchestrator needs it.")
        client = AsyncOpenAI(api_key=settings.cerebras_api_key, base_url=settings.cerebras_base_url)
        model = settings.cerebras_model
    elif provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not configured; the live orchestrator needs it.")
        client = AsyncOpenAI(api_key=settings.groq_api_key, base_url=settings.groq_base_url)
        model = settings.groq_model
    elif provider == "together":
        if not settings.together_api_key:
            raise RuntimeError("TOGETHER_API_KEY is not configured; the live orchestrator needs it.")
        client = AsyncOpenAI(api_key=settings.together_api_key, base_url=settings.together_base_url)
        model = settings.together_model
    else:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured; the live orchestrator needs it.")
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        model = settings.openai_model

    async def complete(messages: list[dict], tools: list[dict]) -> CompletionResult:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model, messages=messages, tools=tools, extra_body=extra_body
            ),
            timeout=settings.llm_call_timeout_sec,
        )
        choice = resp.choices[0].message
        tool_calls: list[ToolCall] = []
        for tc in choice.tool_calls or []:
            try:
                arguments = json.loads(tc.function.arguments or "{}")
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments must be a JSON object")
            except (json.JSONDecodeError, TypeError):
                # Malformed tool-call JSON must not crash the whole
                # completion (Phase 4): substitute a sentinel that will
                # naturally mismatch every real tool's signature, so
                # orchestrator.py's existing TypeError handling turns it
                # into a structured tool-error fed back to the model
                # instead of this exception propagating and ending the
                # session.
                log.warning(
                    "live: tool call %s had malformed JSON arguments: %r",
                    tc.function.name,
                    tc.function.arguments,
                )
                arguments = {"_malformed_tool_arguments": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=arguments))
        return CompletionResult(content=_clean_harmony_leak(choice.content), tool_calls=tool_calls)

    return complete


def _make_live_complete_fn(
    settings: Settings, *, on_retry: Callable[[str, int], Awaitable[None]] | None = None
) -> CompleteFn:
    """The live orchestrator's real CompleteFn (Phase 4): the primary
    provider (``settings.live_llm_provider``) plus retry/backoff, with
    automatic failover to any OTHER fully-configured live provider if the
    primary's own retry budget is exhausted — see ``live/resilience.py``.

    Raises ``RuntimeError`` only if the PRIMARY provider itself isn't
    configured, matching the old single-provider function's exact contract:
    a misconfigured primary is a hard connect-time failure, not something
    silently degraded to whatever fallback happens to be available.
    """
    primary = (settings.live_llm_provider or "openai").lower()
    if primary not in _LIVE_PROVIDERS:
        primary = "openai"
    primary_fn = _make_provider_complete_fn(settings, primary)  # raises RuntimeError if misconfigured

    providers: list[tuple[str, CompleteFn]] = [(primary, primary_fn)]
    for name in _LIVE_PROVIDERS:
        if name == primary or not _live_provider_configured(settings, name):
            continue
        providers.append((name, _make_provider_complete_fn(settings, name)))

    return resilience.make_resilient_complete_fn(providers, on_retry=on_retry)


class _WsSessionFacade:
    """Duck-types AgentSession's ``say``/``shutdown`` surface for
    SessionGuard, unchanged from the LiveKit path. ``say`` pushes a "speak"
    message to the client (mirroring how a turn reply is delivered);
    ``shutdown`` closes the socket, which unblocks the main receive loop
    (below) into its own persist+score path — SessionGuard itself has no
    async close, so the actual WS close is scheduled as a background task.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self.ended = False

    async def say(self, text: str) -> None:
        with contextlib.suppress(Exception):
            await self._ws.send_json(protocol.speak(text))

    def shutdown(self, *, drain: bool = True) -> None:
        self.ended = True
        asyncio.create_task(self._close_quietly())

    async def _close_quietly(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close(code=1000)


@ws_router.websocket("/api/live/session/{session_id}")
async def live_session_ws(websocket: WebSocket, session_id: str) -> None:
    deps: Deps = build_deps()

    view = await deps.repo.get_session_view(session_id)
    if view is None or view.context is None:
        await websocket.close(code=4404)
        return
    if view.status in TERMINAL_STATUSES:
        # A finished interview's record is final (Phase 2's terminal-status
        # protection) — refuse to spin up a phantom second interview whose
        # answers could never actually be saved. Pre-accept close, same
        # shape as the unknown-session case above: there is genuinely
        # nothing here for a new connection to attach to.
        await websocket.close(code=4409)
        return

    # Read the durable resume state BEFORE accepting, so the fresh-vs-resume
    # decision (and everything built from it below) is made once, up front.
    # An empty ``messages`` list means either a session that never opened a
    # connection yet, or one that predates Phase 3 — both are a fresh start,
    # not a resume.
    resume_state = await deps.repo.get_live_resume_state(session_id)
    is_resume = resume_state is not None and bool(resume_state.messages)

    await websocket.accept()

    ud = InterviewUserdata(ctx=view.context, session_id=session_id)
    # Track the version this connection last knew about (Phase 2) — every
    # persist call below supplies it as expected_version, so this session's
    # own repeated checkpoints keep succeeding while a genuinely stale or
    # concurrent writer gets rejected instead of silently overwritten.
    ud.version = view.version
    if resume_state is not None:
        # Restore the FULL durable transcript, not just what this connection
        # will add — SessionGuard's turn-count ceiling reads len(ud.transcript),
        # so under-restoring it would let a candidate raise the effective
        # limit simply by reconnecting.
        ud.transcript = list(resume_state.transcript)

    session = LiveTurnSession(ud=ud)
    if is_resume:
        assert resume_state is not None  # is_resume implies this
        session.persona = resume_state.persona
        session.messages = list(resume_state.messages)

    claimed = await deps.live_session_repo.try_claim(session_id, session)
    if not claimed:
        # Another connection for this session_id is ALREADY live in-process
        # right now (its shutdown sequence hasn't run — e.g. a stale tab the
        # server hasn't yet noticed disconnected, or two tabs open at once).
        # Refuse this one outright rather than let two writers race over the
        # same InterviewUserdata/context — see live_session_repository.py's
        # try_claim docstring.
        with contextlib.suppress(Exception):
            await websocket.send_json(
                protocol.session_conflict(
                    "This interview session is already connected elsewhere."
                )
            )
        await websocket.close(code=4409)
        return

    async def _on_llm_retry(provider: str, attempt: int) -> None:
        # Candidate-safe: the event carries only the attempt number, never
        # the provider name (internal infra detail — see Phase 6's
        # candidate-safe-error posture).
        with contextlib.suppress(Exception):
            await websocket.send_json(protocol.llm_retrying(attempt))

    try:
        complete_fn = _make_live_complete_fn(deps.settings, on_retry=_on_llm_retry)
    except RuntimeError as exc:
        # exc's own text names the missing env var (e.g. "CEREBRAS_API_KEY is
        # not configured") — genuinely useful in a server log, never
        # candidate-safe to put on the wire (Phase 6): it confirms which
        # internal provider is wired up and exactly how the deployment is
        # misconfigured. Log the real detail, send a generic message.
        log.error("live: cannot start session %s — configuration error: %s", session_id, exc)
        capture_error(exc)
        await websocket.send_json(
            protocol.error(
                "The interview service is temporarily unavailable. Please try again shortly.",
                code="SERVICE_UNAVAILABLE",
            )
        )
        await websocket.close(code=1011)
        # Release the claim we just took — otherwise this session_id is
        # permanently unconnectable until process restart (the original,
        # still-open leak this rewrite also closes: a config failure used to
        # leave live_session_repo holding an entry no shutdown path would
        # ever ``put()``, since that used to happen unconditionally too).
        await deps.live_session_repo.delete(session_id)
        return

    facade = _WsSessionFacade(websocket)
    lang_mode = view.context.plan.language_mode
    guard = SessionGuard(
        facade,
        ud,
        max_duration_sec=deps.settings.max_interview_duration_sec,
        max_turns=deps.settings.max_interview_turns,
        wrap_up_line=wrap_up_line(lang_mode.primary),
    )

    async def _checkpoint() -> None:
        # Off-path checkpoint so GET /api/session/{id} (which the room UI
        # polls for questionText/cursor/turn history) reflects progress
        # DURING the interview, not just once it ends — without this the
        # candidate hears new questions but the UI never advances past
        # question 1 until the whole session shuts down. Also carries the
        # live conversation (Phase 3) so a later reconnect can resume it.
        # Network/other failures are still swallowed here (a flaky
        # checkpoint POST must never break the live turn loop) — but a
        # version conflict is a REAL signal, not noise: it means some other
        # writer touched this session's persisted state since we last read
        # it. Logged distinctly rather than folded into a blanket suppress.
        try:
            new_version = await flush_checkpoint(
                session_id,
                ud.ctx,
                ud.transcript,
                deps.settings,
                expected_version=ud.version,
                conversation={"persona": session.persona, "messages": session.messages},
            )
        except Exception as exc:
            log.exception("live: checkpoint failed for %s", session_id)
            capture_error(exc)
        else:
            if new_version is not None:
                ud.version = new_version
            else:
                log.warning(
                    "live: checkpoint for %s did not update (stale version %s or "
                    "session already terminal) — a concurrent writer may exist",
                    session_id,
                    ud.version,
                )

    async def _shutdown_sequence() -> None:
        await guard.aclose()
        # background_score=True: release ownership as soon as PERSISTENCE is
        # durable, not after scoring (which can run for minutes) also
        # finishes — otherwise a candidate who disconnects mid-interview and
        # reconnects gets wrongly rejected with session_conflict, blocked
        # behind their own still-running scoring job. See persist_and_score's
        # docstring (found via a real production smoke test).
        await persist_and_score(session_id, ud, deps, background_score=True)
        await deps.live_session_repo.delete(session_id)

    async def _safe_open_interview() -> TurnResult | None:
        """None means every provider's full retry budget is exhausted
        (Phase 4) — the caller decides how to degrade gracefully instead of
        this propagating to the outer handler and ending the interview."""
        try:
            return await orch.open_interview(session, complete_fn)
        except Exception as exc:
            log.exception(
                "live: opening turn failed for %s (retries/failover exhausted)", session_id
            )
            capture_error(exc)
            return None

    async def _safe_run_turn(text: str) -> TurnResult | None:
        """Same contract as _safe_open_interview, for a candidate turn."""
        try:
            return await orch.run_turn(session, text, complete_fn)
        except Exception as exc:
            log.exception(
                "live: turn failed for %s (retries/failover exhausted)", session_id
            )
            capture_error(exc)
            return None

    try:
        if is_resume:
            # Skip open_interview()'s greeting entirely — the candidate
            # already heard it. The client already knows what's current via
            # its own GET /api/session/{id} poll; the model's next actual
            # reply (once the candidate speaks) naturally continues the
            # restored conversation since session.messages carries it.
            await websocket.send_json(protocol.session_resumed())
        else:
            await websocket.send_json(protocol.session_connected())
            opening = await _safe_open_interview()
            if opening is None:
                # Every provider failed to even open the interview — nothing
                # has been checkpointed yet, so there's no partial state to
                # protect. Tell the candidate plainly and let them reconnect
                # (Phase 3's is_resume stays False, so a retry cleanly
                # attempts the same opening flow again) rather than pretend
                # the connection is still useful.
                await websocket.send_json(
                    protocol.temporary_failure(
                        "We're having trouble starting the interview right now. "
                        "Please try reconnecting in a moment."
                    )
                )
                await websocket.close(code=1011)
                return
            await websocket.send_json(protocol.speak(opening.reply_text))
            # Durably save turn-0 state immediately: without this, a
            # candidate who disconnects right after the greeting — before
            # ever answering, so the loop below never runs — reconnects to
            # an EMPTY live_conversation and gets re-greeted instead of
            # resumed.
            await _checkpoint()
        guard.start()

        while not session.should_end and not facade.ended:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "utterance_end":
                continue  # partial_transcript etc. — no server action in Phase 3
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            if not _ws_turn_limiter.allow(session_id):
                await websocket.send_json(
                    protocol.rate_limited(_ws_turn_limiter.retry_after(session_id))
                )
                continue
            if len(text) > _MAX_UTTERANCE_LEN:
                # Rejected outright, never silently truncated — a truncated
                # answer would be scored/recorded as something the
                # candidate didn't actually say.
                await websocket.send_json(
                    protocol.input_rejected(
                        "That message was too long — please keep answers to a "
                        "reasonable length."
                    )
                )
                continue
            result = await _safe_run_turn(text)
            if result is None:
                # Every provider failed for THIS turn — the candidate's
                # utterance is already recorded in the transcript (state.
                # add_turn ran before the failed completion call), so
                # nothing is lost; let them try again rather than ending a
                # session that's otherwise perfectly healthy.
                await websocket.send_json(
                    protocol.temporary_failure(
                        "Sorry, I'm having trouble right now. Could you say that again "
                        "in a moment?"
                    )
                )
                continue
            if result.reply_text:
                await websocket.send_json(protocol.speak(result.reply_text))
            await _checkpoint()
            if result.should_end:
                break

        # Loop exited because should_end flipped True (end_interview tool
        # call), not because the client disconnected — explicitly close so
        # the client gets a real close frame instead of a handler that just
        # returns and leaves the connection hanging open with nothing more
        # ever arriving.
        if not facade.ended:
            await websocket.close(code=1000)
    except WebSocketDisconnect:
        log.info("live: session %s disconnected", session_id)
    except Exception as exc:
        # Anything reaching here is NOT an LLM completion failure (those are
        # already contained by _safe_open_interview/_safe_run_turn above) —
        # a genuine bug or an unexpected transport error. Still never let it
        # go unreported: log with full context and forward to Sentry/etc. if
        # configured (Phase 8), same as every other previously-silent
        # exception site in this handler.
        log.exception("live: session %s WS handler error", session_id)
        capture_error(exc)
    finally:
        await _shutdown_sequence()
