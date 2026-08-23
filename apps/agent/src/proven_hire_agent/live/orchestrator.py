"""The LiveKit-free turn-taking/tool-calling loop (Live Voice Pipeline Replacement).

Replaces ``AgentSession``'s "listen -> call the LLM with tools -> execute a
tool or speak" loop, which LiveKit provided for free. This module is
**pure/livekit-free by design** — no import of ``livekit`` anywhere, no
network calls except the injected LLM completion function — so the whole
turn-taking logic is unit-testable exactly like ``state.py`` already is,
before Phase 3 wires it to a real WebSocket + real OpenAI calls.

Design carried over unchanged from ``live/interviewer.py``/``live/handoffs.py``
(only the LiveKit-specific plumbing was replaced):
- The prompt injects ONLY the compact candidate summary + current question +
  recent turns — never the whole CV/JD/company (project golden rule #2).
- Tool functions mutate ``LiveTurnSession``/``InterviewUserdata`` ONLY. No
  network/DB on the turn path; persistence + scoring happen on shutdown
  (ported into Phase 3's WS handler from ``worker.py``'s ``_on_shutdown``).
- ``submit_answer`` saves AND advances in one atomic call — this merge fixed
  a real production bug (2026-08-17) where the interview got stuck because
  the model reliably made a save call but not always a separate advance call.

One thing NOT carried over, and a real regression until this fix: LiveKit's
``on_enter``/handoff plumbing gave the old path a free ``conversation_item_added``
event for every committed user/assistant turn, which ``worker.py``'s
``wire_transcript_capture`` used to populate ``InterviewUserdata.transcript``.
This transport has no event system, so ``run_turn``/``_run_loop`` call
``state.add_turn`` directly at the two points where real (spoken, not
intermediate tool-call) text is known. Until this was wired, every live
interview's transcript was permanently empty on this transport, which
silently disabled ``state.reconstruct_answers`` (the answer-recovery safety
net), ``SessionGuard``'s turn-count ceiling, and transcript persistence.

English-first v1 (see the rework plan): OpenAI-only tool-calling. A
Gemini-live adapter is explicitly deferred, not built here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from ..core.logging import get_logger
from . import state
from .state import InterviewUserdata

if TYPE_CHECKING:
    from ..shared_models import PlannedQuestion

log = get_logger(__name__)

Persona = Literal["interviewer", "coding", "behavioral"]

_MAX_TOOL_ROUNDS = 4  # bounded loop guard — a tool call can request another
# tool call (e.g. get_difficulty_hint then submit_answer), but never forever.

# Absolute last resort if even the forced no-tools completion (see _run_loop's
# bounded-loop safety valve) also fails — the candidate must never experience
# total silence after speaking. Deliberately generic/English-only: reaching
# this line means the model itself is unavailable, so there is no live
# language-aware generation to draw from.
_SAFETY_VALVE_FALLBACK_LINE = (
    "Sorry, I'm having a little trouble right now — could you give me just a moment?"
)


@dataclass
class LiveTurnSession:
    """Per-connection state for the custom orchestrator — wraps the same
    ``InterviewUserdata`` the old LiveKit path used, unchanged, plus the two
    things ``AgentSession`` used to own: which persona is active (LiveKit's
    native agent-handoff equivalent) and the running chat history (LiveKit's
    ``chat_ctx``, which handoffs carried across automatically because it's
    just a plain list here, never torn down).
    """

    ud: InterviewUserdata
    persona: Persona = "interviewer"
    messages: list[dict[str, Any]] = field(default_factory=list)
    should_end: bool = False


@dataclass(frozen=True)
class TurnResult:
    """What one call to :func:`run_turn`/:func:`open_interview` produced."""

    reply_text: str
    should_end: bool


# --- pure prompt-building helpers (ported verbatim from interviewer.py) ------


def _localized(text: dict[str, str], primary: str) -> str:
    """Resolve a ``LocalizedText`` to the primary language, falling back to en."""
    return text.get(primary) or text.get("en") or next(iter(text.values()), "")


def _wrap_signal() -> str:
    return (
        "INTERVIEW_COMPLETE: thank the candidate warmly in one or two sentences, "
        "tell them their feedback report will be ready shortly, and then call "
        "end_interview to close the session."
    )


def _followup_note(q: PlannedQuestion | None) -> str:
    """Surface the plan's seeded ``followups`` (see ``prep/follow_up_depth.py``)
    so the live model uses the planned hint instead of improvising one blind."""
    followups = list(q.followups) if q is not None else []
    if not followups:
        return ""
    if len(followups) == 1:
        return (
            "\nPlanned follow-up (use verbatim if the candidate needs a nudge "
            f"— don't invent your own): {followups[0]}\n"
        )
    numbered = "\n".join(f"  {i + 1}. {f}" for i, f in enumerate(followups))
    return (
        f"\nThis question has {len(followups)} planned follow-ups. After the "
        "candidate's initial answer, work through them adaptively based on "
        "what they said (skip ones already covered, stop once you have a "
        f"solid answer — you don't have to use all {len(followups)}), "
        f"roughly in order:\n{numbered}\n"
    )


_CODING_INSTRUCTIONS = (
    "You are now running the CODING round. Pose one focused, hands-on problem "
    "tied to the candidate's stack, stated in 2-3 short spoken sentences — "
    "never stack the setup and multiple sub-asks into one long sentence. Ask "
    "them to think aloud; nudge with a single hint if they stall. If a planned "
    "follow-up/hint is shown in your instructions above, use it verbatim "
    "instead of inventing one. Do not lecture. When the problem is resolved or "
    "time is tight, call submit_answer to record it and move on."
)

_BEHAVIORAL_INSTRUCTIONS = (
    "You are now running the BEHAVIORAL round. Ask one STAR-style question at a "
    "time about real past experience, in one short sentence. Listen, then ask "
    "exactly one probing follow-up for specifics (the 'I' not the 'we'), also "
    "one short sentence. If a planned follow-up is shown in your instructions "
    "above, use it verbatim — it's already written to target their individual "
    "contribution. Then call submit_answer to record it and move on. Warm, "
    "concise, never leading."
)

_PERSONA_INSTRUCTIONS: dict[Persona, str] = {
    "interviewer": "",
    "coding": _CODING_INSTRUCTIONS,
    "behavioral": _BEHAVIORAL_INSTRUCTIONS,
}


def build_instructions(session: LiveTurnSession) -> str:
    """Lean per-question system prompt: compact summary + current question
    (+ persona-specific instructions once a round handoff has happened)."""
    ud = session.ud
    primary = ud.ctx.plan.language_mode.primary
    summary = state.compact_summary(ud)
    q = state.current_question(ud)
    question_line = (
        _localized(q.text, primary) if q is not None else "(no further questions)"
    )
    base = (
        "You are a senior, friendly technical interviewer running a real-time "
        "voice mock interview. Speak naturally and concisely — the candidate "
        "HEARS you, they aren't reading text, so keep every question you ask "
        "(the current one below AND any follow-up you improvise) to ONE clear, "
        "short ask, roughly a sentence. If a natural next question would need "
        "two things asked at once, ask the first now and save the second for "
        "your next follow-up instead of combining them into one long compound "
        "question — a candidate can't hold a long multi-part question in their "
        "head while answering.\n\n"
        f"{summary}\n\n"
        f"Primary language: {primary}.\n"
        f"Current question to ask: {question_line}\n"
        f"{_followup_note(q)}\n"
        "Ask this one question, listen to the full answer, then ask at most one "
        "light follow-up (unless a follow-up tree is given above — see its own "
        "instructions). If the answer feels thin or hedgy and you're unsure "
        "whether to probe further, you may call get_followup_signal for an "
        "advisory read. When the answer is complete, call submit_answer with the "
        "candidate's answer to record it and move to the next question. Use "
        "next_section to move to a different round, request_clarification only if "
        "the candidate seems confused. Never read the rubric aloud."
    )
    extra = _PERSONA_INSTRUCTIONS[session.persona]
    return f"{base}\n\n{extra}" if extra else base


def opening_instructions(session: LiveTurnSession) -> str:
    """The one-shot instruction for the FIRST turn (LiveKit's ``on_enter``
    equivalent). Greets on the base interviewer persona; a bare round
    transition (no re-greeting) once a handoff has happened."""
    ud = session.ud
    primary = ud.ctx.plan.language_mode.primary
    q = state.current_question(ud)
    question_line = (
        _localized(q.text, primary) if q is not None else "(no further questions)"
    )
    if session.persona == "interviewer":
        first_name = (ud.ctx.candidate.name or "there").split()[0]
        return (
            f"Open the interview now, speaking in {primary}. Warmly greet the "
            f"candidate by first name ({first_name}) in one short sentence and "
            f"say you'll be running their mock interview for the {ud.ctx.job.title} "
            f"role at {ud.ctx.job.company_name}. Then ask this first question and "
            f"stop: {question_line}. Keep it natural and concise; do not call any "
            "tools yet."
        )
    label = "coding" if session.persona == "coding" else "behavioral"
    return (
        f"Transition smoothly into the {label} round, speaking in {primary}. In "
        "one short sentence say you're moving to this round, then ask this "
        f"question and stop: {question_line}. Do not re-introduce yourself or "
        "call any tools yet."
    )


async def _refresh_note(session: LiveTurnSession) -> str:
    """Re-sync note appended after a cursor-advancing tool call so the NEXT
    LLM call's system prompt (rebuilt fresh from build_instructions on every
    turn — there is no persistent "instructions" object to mutate the way
    LiveKit's ``Agent.update_instructions`` did) reflects the new question.
    Kept as a no-op placeholder for symmetry with the ported original; the
    caller always rebuilds instructions from session state on every turn, so
    nothing is actually stale here unlike the LiveKit version's concern."""
    return ""


# --- tool functions (plain async functions; ported from interviewer.py's
# @function_tool methods — same names/behavior, no Agent/RunContext) ---------


async def submit_answer(
    session: LiveTurnSession, answer: str, started_at: str = "", ended_at: str = ""
) -> str:
    """Record the candidate's answer to the current question AND advance to
    the next one, in a single call. See interviewer.py's original docstring
    for why this must stay merged (a real 2026-08-17 production bug)."""
    ud = session.ud
    state.save_answer(ud, transcript=answer, started_at=started_at, ended_at=ended_at)
    state.advance(ud)
    if state.is_complete(ud):
        return _wrap_signal()
    q = state.current_question(ud)
    assert q is not None
    primary = ud.ctx.plan.language_mode.primary
    text = _localized(q.text, primary)
    return f"Saved. Next question ({q.section}): {text}"


async def next_section(session: LiveTurnSession) -> str:
    """Skip to the first question of the next section (or wrap if none)."""
    ud = session.ud
    q = state.next_section(ud)
    if q is None:
        return _wrap_signal()
    primary = ud.ctx.plan.language_mode.primary
    text = _localized(q.text, primary)
    return f"Moving to {q.section}: {text}"


async def get_difficulty_hint(session: LiveTurnSession) -> str:
    """Advisory hint on whether to go harder/easier, advance, or wrap."""
    return state.difficulty_hint(session.ud)


async def get_followup_signal(
    session: LiveTurnSession, answer_so_far: str, followups_asked_so_far: int = 0
) -> str:
    """Advisory read on whether the candidate's answer needs a follow-up."""
    sig = state.evaluate_followup_need(session.ud, answer_so_far, followups_asked_so_far)
    verdict = "follow up" if sig.should_follow_up else "move on"
    return f"{verdict}: {sig.reason}"


async def request_clarification(session: LiveTurnSession, reason: str = "") -> str:
    """Note that the candidate needs the current question rephrased."""
    ud = session.ud
    q = state.current_question(ud)
    if q is None:
        return "No active question to clarify."
    primary = ud.ctx.plan.language_mode.primary
    return (
        "Rephrase the current question more simply, keeping the same intent: "
        f"{_localized(q.text, primary)}"
    )


async def end_interview(session: LiveTurnSession) -> str:
    """End the interview session AFTER saying goodbye.

    Unlike the LiveKit version (which called ``self.session.shutdown(drain=
    True)`` directly), this only sets ``session.should_end`` — Phase 3's WS
    handler is the one with an actual transport/connection to drain and
    close, and the one that triggers the persist+score shutdown flow ported
    from ``worker.py``'s ``_on_shutdown``. Keeping this function pure/
    side-effect-free onto ``session`` only is what makes it unit-testable
    with zero network/WS involved.
    """
    session.should_end = True
    return "Interview ended. Say nothing further."


async def start_coding_round(session: LiveTurnSession) -> str:
    """Switch to the coding-round persona (replaces LiveKit's native
    Agent-handoff mechanism — here it's just a field flip, since messages
    are already one shared list, never per-persona like LiveKit's chat_ctx
    forwarding required)."""
    session.persona = "coding"
    return "Switched to the coding round."


async def start_behavioral_round(session: LiveTurnSession) -> str:
    """Switch to the behavioral-round persona. See start_coding_round."""
    session.persona = "behavioral"
    return "Switched to the behavioral round."


# --- tool schemas (OpenAI native function-calling) + registry ---------------

TOOL_REGISTRY: dict[str, Callable[..., Awaitable[str]]] = {
    "submit_answer": submit_answer,
    "next_section": next_section,
    "get_difficulty_hint": get_difficulty_hint,
    "get_followup_signal": get_followup_signal,
    "request_clarification": request_clarification,
    "end_interview": end_interview,
    "start_coding_round": start_coding_round,
    "start_behavioral_round": start_behavioral_round,
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "submit_answer",
            "description": submit_answer.__doc__.strip().splitlines()[0],
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string", "description": "The candidate's spoken answer text."},
                    "started_at": {"type": "string", "description": "ISO-8601 timestamp, optional."},
                    "ended_at": {"type": "string", "description": "ISO-8601 timestamp, optional."},
                },
                "required": ["answer"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "next_section",
            "description": next_section.__doc__.strip(),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_difficulty_hint",
            "description": get_difficulty_hint.__doc__.strip(),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_followup_signal",
            "description": get_followup_signal.__doc__.strip().splitlines()[0],
            "parameters": {
                "type": "object",
                "properties": {
                    "answer_so_far": {"type": "string"},
                    "followups_asked_so_far": {"type": "integer", "default": 0},
                },
                "required": ["answer_so_far"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_clarification",
            "description": request_clarification.__doc__.strip(),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string", "default": ""}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_interview",
            "description": end_interview.__doc__.strip().splitlines()[0],
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_coding_round",
            "description": start_coding_round.__doc__.strip().splitlines()[0],
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_behavioral_round",
            "description": start_behavioral_round.__doc__.strip().splitlines()[0],
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# --- the turn loop ------------------------------------------------------------
#
# ``complete_fn`` is injected (not a hardcoded OpenAI client call) so this
# whole loop is unit-testable with a fake completion function, exactly like
# SessionGuard takes an injectable ``time_fn``. Phase 3 wires the real
# OpenAI ``AsyncOpenAI().chat.completions.create`` call in here.
#
# Shape: async (messages: list[dict], tools: list[dict]) -> CompletionResult
# where CompletionResult has EITHER .content (str) OR .tool_calls (list of
# {id, name, arguments: dict}) — a minimal duck-typed shape so tests don't
# need the real openai SDK installed.


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class CompletionResult:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


CompleteFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Awaitable[CompletionResult]]


async def _run_loop(session: LiveTurnSession, complete_fn: CompleteFn) -> TurnResult:
    """Bounded "call the LLM, execute any tool calls, repeat" loop, shared by
    :func:`open_interview` and :func:`run_turn`."""
    for _ in range(_MAX_TOOL_ROUNDS):
        messages = [{"role": "system", "content": build_instructions(session)}, *session.messages]
        result = await complete_fn(messages, TOOL_SCHEMAS)

        if not result.tool_calls:
            text = result.content or ""
            session.messages.append({"role": "assistant", "content": text})
            # This is the only point where the model produces text that
            # actually gets spoken (session.messages's tool-call/tool-result
            # entries below never reach TTS) — the real-transport equivalent
            # of LiveKit's "assistant" conversation_item_added event. Tagged
            # AFTER any tool calls this same loop already executed, so it
            # correctly reflects the question now current (e.g. the NEW
            # question just asked), not the one the candidate was answering.
            if text:
                state.add_turn(session.ud, "assistant", text)
            return TurnResult(reply_text=text, should_end=session.should_end)

        # OpenAI requires the assistant tool-call message to precede the tool
        # result messages in the history.
        session.messages.append(
            {
                "role": "assistant",
                "content": result.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        # OpenAI requires arguments to be a JSON-encoded STRING
                        # in the message history, not a raw object — this is
                        # the shape it echoes back to us on ToolCall.arguments,
                        # but not the shape it accepts when we replay it.
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in result.tool_calls
                ],
            }
        )
        for tc in result.tool_calls:
            fn = TOOL_REGISTRY.get(tc.name)
            if fn is None:
                tool_result = f"Unknown tool: {tc.name}"
            else:
                try:
                    tool_result = await fn(session, **tc.arguments)
                except TypeError as exc:
                    # The model called a real tool with missing/unexpected
                    # arguments — including api/live.py's malformed-JSON
                    # sentinel, which is deliberately shaped to mismatch
                    # every real tool signature and land here. Feed a
                    # structured error back so it can retry correctly,
                    # instead of this crashing the whole turn.
                    log.warning(
                        "live: tool %s called with invalid arguments %r: %s",
                        tc.name,
                        tc.arguments,
                        exc,
                    )
                    tool_result = (
                        f"Error calling {tc.name}: invalid arguments. Check the "
                        "required parameters and try again."
                    )
                except Exception:
                    # A bug in the tool implementation itself, or an
                    # unexpected failure inside it — never let this crash
                    # the turn loop, and never echo the raw exception back
                    # to the model (it could contain internal detail).
                    log.exception("live: tool %s raised an unexpected error", tc.name)
                    tool_result = f"Error calling {tc.name}: an internal error occurred. Try a different approach."
            session.messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": tool_result}
            )
        # Loop again: the model gets the tool results and either replies in
        # text or calls another tool (e.g. get_difficulty_hint then submit_answer).

    # Bounded-loop safety valve: a model stuck calling tools forever must
    # never leave the candidate in silence. Force ONE final completion with
    # no tools offered, so it has to produce a real spoken reply; if even
    # that fails, fall back to a generic line rather than empty text.
    log.warning(
        "live: tool-round budget (%d) exhausted for session %s; forcing a "
        "final no-tools completion",
        _MAX_TOOL_ROUNDS,
        session.ud.session_id,
    )
    messages = [{"role": "system", "content": build_instructions(session)}, *session.messages]
    try:
        final = await complete_fn(messages, [])
        text = final.content or _SAFETY_VALVE_FALLBACK_LINE
    except Exception:
        log.exception(
            "live: forced final completion also failed for session %s after "
            "tool-round exhaustion",
            session.ud.session_id,
        )
        text = _SAFETY_VALVE_FALLBACK_LINE
    session.messages.append({"role": "assistant", "content": text})
    state.add_turn(session.ud, "assistant", text)
    return TurnResult(reply_text=text, should_end=session.should_end)


async def open_interview(session: LiveTurnSession, complete_fn: CompleteFn) -> TurnResult:
    """The first turn — LiveKit's ``on_enter`` equivalent. No candidate input
    yet; drives the greeting + Q1 (or round-transition) opener."""
    session.messages.append({"role": "user", "content": opening_instructions(session)})
    return await _run_loop(session, complete_fn)


async def run_turn(
    session: LiveTurnSession, candidate_utterance: str, complete_fn: CompleteFn
) -> TurnResult:
    """One turn: the candidate said ``candidate_utterance`` (the STT
    ``UtteranceEnd``-committed text — see the client InterviewSession class),
    the LLM responds, possibly calling tools along the way."""
    # Real committed candidate speech — the transport-agnostic equivalent of
    # LiveKit's "user" conversation_item_added event. Tagged with the question
    # active RIGHT NOW, before any tool call in this turn can advance the
    # cursor, so it's tied to the question the candidate was actually
    # answering (see state.add_turn's own docstring for why order matters).
    state.add_turn(session.ud, "user", candidate_utterance)
    session.messages.append({"role": "user", "content": candidate_utterance})
    return await _run_loop(session, complete_fn)
