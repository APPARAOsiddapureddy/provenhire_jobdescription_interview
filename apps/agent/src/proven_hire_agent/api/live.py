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

import httpx
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict

from ..core.config import Settings
from ..core.deps import Deps, build_deps
from ..core.logging import get_logger
from ..core.session_status import TERMINAL_STATUSES
from ..live import orchestrator as orch
from ..live import protocol
from ..live.guard import SessionGuard, wrap_up_line
from ..live.orchestrator import CompleteFn, CompletionResult, LiveTurnSession, ToolCall
from ..live.persistence import flush_checkpoint, persist_and_score
from ..live.state import InterviewUserdata

log = get_logger(__name__)

router = APIRouter()
ws_router = APIRouter()

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
async def mint_deepgram_token() -> DeepgramTokenResponse:
    settings = build_deps().settings
    if not settings.deepgram_api_key:
        raise HTTPException(status_code=503, detail="Deepgram is not configured")

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
async def synthesize_tts(req: TtsRequest):
    from fastapi import Response

    settings = build_deps().settings
    if not settings.cartesia_api_key:
        raise HTTPException(status_code=503, detail="Cartesia TTS is not configured")
    if len(req.text) > _MAX_TTS_TEXT_LEN:
        raise HTTPException(status_code=413, detail="Text too long for one TTS call")

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


def _make_openai_complete_fn(settings: Settings) -> CompleteFn:
    """Real CompleteFn for the live orchestrator (English-first v1 — see the
    rework plan; a Gemini adapter is explicitly deferred).

    Backend selectable via ``settings.live_llm_provider`` — "openai" (default),
    "cerebras", "groq", or "together". The three alternates are OpenAI-API-
    compatible fast/cheap inference providers (same SDK, same tool-calling
    shape, just a ``base_url``/key/model swap) measured materially faster for
    this specific tool-calling loop (~7-9s -> ~2-3s per turn) at much lower
    cost, hence the default-off opt-in rather than replacing OpenAI outright.
    Three alternates are wired in because account-activation issues (billing
    holds, signup fraud flags) turned out to be the real obstacle in
    practice, not model/API availability.
    """
    from openai import AsyncOpenAI

    provider = (settings.live_llm_provider or "openai").lower()
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
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments or "{}"))
            for tc in (choice.tool_calls or [])
        ]
        return CompletionResult(content=_clean_harmony_leak(choice.content), tool_calls=tool_calls)

    return complete


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

    try:
        complete_fn = _make_openai_complete_fn(deps.settings)
    except RuntimeError as exc:
        await websocket.send_json(protocol.error(str(exc)))
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
        except Exception:
            log.exception("live: checkpoint failed for %s", session_id)
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
        await persist_and_score(session_id, ud, deps)
        await deps.live_session_repo.delete(session_id)

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
            opening = await orch.open_interview(session, complete_fn)
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
            result = await orch.run_turn(session, text, complete_fn)
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
    except Exception:
        log.exception("live: session %s WS handler error", session_id)
    finally:
        await _shutdown_sequence()
