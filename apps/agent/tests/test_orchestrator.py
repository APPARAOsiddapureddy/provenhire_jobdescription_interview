"""Offline tests for the LiveKit-free turn-taking/tool-calling orchestrator
(Live Voice Pipeline Replacement, Phase 2).

Same convention as ``test_live.py``: a real ``InterviewContext`` from the
offline prep pipeline (MockLLM, no network), then exercise the orchestrator's
tool functions and turn loop directly — with a FAKE ``complete_fn`` standing
in for the real OpenAI call, so this whole suite never needs the ``openai``
package installed or a network connection, exactly like ``test_guard.py``
injects a fake clock instead of real time.
"""

from __future__ import annotations

import asyncio

from proven_hire_agent.core.deps import build_deps
from proven_hire_agent.live import orchestrator as orch
from proven_hire_agent.live.orchestrator import (
    CompletionResult,
    LiveTurnSession,
    ToolCall,
)
from proven_hire_agent.live.state import InterviewUserdata
from proven_hire_agent.prep import run_prep
from proven_hire_agent.shared_models import InterviewContext, LanguageMode, PrepRequest


def _build_context() -> InterviewContext:
    deps = build_deps()
    req = PrepRequest(
        cv_url="https://example.com/cv.pdf",
        jd_text="Senior Backend Engineer building distributed payment systems in Python.",
        company="ExampleCorp",
        language_mode=LanguageMode(primary="en", mixed=False),
    )
    session_id = asyncio.run(run_prep(req, deps))
    ctx = asyncio.run(deps.repo.load_context(session_id))
    assert ctx is not None
    return ctx


def _session() -> LiveTurnSession:
    ctx = _build_context()
    ud = InterviewUserdata(ctx=ctx, session_id=ctx.session_id)
    return LiveTurnSession(ud=ud)


# --- tool functions (direct calls, no LLM involved) --------------------------


def test_submit_answer_saves_and_advances() -> None:
    session = _session()
    ud = session.ud
    q1 = ud.ctx.plan.questions[0]

    result = asyncio.run(orch.submit_answer(session, "I led the migration to a sharded ledger."))

    assert ud.ctx.answers[-1].question_id == q1.id
    assert ud.ctx.answers[-1].transcript.startswith("I led the migration")
    assert ud.ctx.cursor == 1
    assert "Saved" in result


def test_submit_answer_on_last_question_returns_wrap_signal() -> None:
    session = _session()
    session.ud.ctx.cursor = len(session.ud.ctx.plan.questions) - 1

    result = asyncio.run(orch.submit_answer(session, "final answer"))

    assert session.ud.ctx.cursor == len(session.ud.ctx.plan.questions)
    assert "INTERVIEW_COMPLETE" in result


def test_next_section_moves_cursor_to_new_section() -> None:
    session = _session()
    starting = session.ud.ctx.plan.questions[0].section

    result = asyncio.run(orch.next_section(session))

    q = session.ud.ctx.plan.questions[session.ud.ctx.cursor] if session.ud.ctx.cursor < len(
        session.ud.ctx.plan.questions
    ) else None
    assert q is None or q.section != starting
    assert "Moving to" in result or "INTERVIEW_COMPLETE" in result


def test_get_difficulty_hint_is_a_plain_string() -> None:
    session = _session()
    hint = asyncio.run(orch.get_difficulty_hint(session))
    assert isinstance(hint, str) and ":" in hint


def test_get_followup_signal_reports_thin_answer() -> None:
    session = _session()
    signal = asyncio.run(orch.get_followup_signal(session, "maybe I guess"))
    assert "follow up" in signal or "move on" in signal


def test_request_clarification_rephrases_current_question() -> None:
    session = _session()
    note = asyncio.run(orch.request_clarification(session, reason="too fast"))
    assert "Rephrase" in note


def test_end_interview_sets_should_end_without_touching_transport() -> None:
    session = _session()
    assert session.should_end is False
    result = asyncio.run(orch.end_interview(session))
    assert session.should_end is True
    assert "ended" in result.lower()


def test_start_coding_round_switches_persona() -> None:
    session = _session()
    assert session.persona == "interviewer"
    asyncio.run(orch.start_coding_round(session))
    assert session.persona == "coding"
    assert "coding" in orch.build_instructions(session).lower()


def test_start_behavioral_round_switches_persona() -> None:
    session = _session()
    asyncio.run(orch.start_behavioral_round(session))
    assert session.persona == "behavioral"
    assert "behavioral" in orch.build_instructions(session).lower()


# --- the turn loop, driven by a fake LLM completion function -----------------


def test_open_interview_with_plain_text_reply() -> None:
    """A text-only completion ends the loop after one round and records the
    opening instruction + reply in the message history."""
    session = _session()

    async def fake_complete(messages, tools):
        return CompletionResult(content="Hi Jordan! Let's get started. <asks Q1>")

    result = asyncio.run(orch.open_interview(session, fake_complete))

    assert result.reply_text.startswith("Hi Jordan")
    assert result.should_end is False
    # user (opening instructions) + assistant (reply)
    assert session.messages[0]["role"] == "user"
    assert session.messages[-1] == {"role": "assistant", "content": result.reply_text}


def test_run_turn_executes_a_tool_call_then_replies() -> None:
    """First completion requests submit_answer; the loop executes it (really
    advancing the cursor via the pure state.py functions) and feeds the tool
    result back for a second completion that finally replies in text."""
    session = _session()
    calls = {"n": 0}

    async def fake_complete(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="call_1", name="submit_answer", arguments={"answer": "Ledger service work."})
                ]
            )
        return CompletionResult(content="Great, next question incoming.")

    result = asyncio.run(orch.run_turn(session, "I built a ledger service.", fake_complete))

    assert calls["n"] == 2
    assert session.ud.ctx.cursor == 1  # the tool call really advanced state.py's cursor
    assert result.reply_text == "Great, next question incoming."
    # Message history has the tool-call assistant turn AND the tool result.
    roles = [m["role"] for m in session.messages]
    assert "tool" in roles


def test_run_loop_bounded_safety_valve_never_hangs() -> None:
    """A model that keeps calling tools forever must not hang the turn —
    the bounded loop stops after _MAX_TOOL_ROUNDS and returns an empty reply
    rather than looping indefinitely."""
    session = _session()

    async def fake_complete(messages, tools):
        return CompletionResult(
            tool_calls=[ToolCall(id="call_x", name="get_difficulty_hint", arguments={})]
        )

    result = asyncio.run(orch.run_turn(session, "an answer", fake_complete))

    assert result.reply_text == ""


def test_end_interview_tool_call_propagates_should_end_through_the_loop() -> None:
    session = _session()

    async def fake_complete(messages, tools):
        if not any(m.get("role") == "tool" for m in messages):
            return CompletionResult(
                tool_calls=[ToolCall(id="call_1", name="end_interview", arguments={})]
            )
        return CompletionResult(content="Goodbye!")

    result = asyncio.run(orch.run_turn(session, "that's all from me", fake_complete))

    assert session.should_end is True
    assert result.should_end is True
    assert result.reply_text == "Goodbye!"
