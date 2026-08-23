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
from proven_hire_agent.live import state
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


# --- bounded LLM context growth (Phase 7, backend hardening plan) -----------


def test_trimmed_messages_returns_everything_under_the_limit() -> None:
    session = _session()
    session.messages = [{"role": "user", "content": f"turn {i}"} for i in range(5)]

    trimmed = orch._trimmed_messages(session)

    assert trimmed == session.messages
    assert trimmed is not session.messages  # a copy, not the live list


def test_trimmed_messages_keeps_only_recent_turns_once_over_the_limit() -> None:
    session = _session()
    # Every entry is its own clean "user" boundary, so the trim point is
    # unambiguous: exactly the last _MAX_CONTEXT_MESSAGES entries.
    session.messages = [{"role": "user", "content": f"turn {i}"} for i in range(100)]

    trimmed = orch._trimmed_messages(session)

    assert len(trimmed) == orch._MAX_CONTEXT_MESSAGES
    assert trimmed[-1] == {"role": "user", "content": "turn 99"}
    assert trimmed[0] == {"role": "user", "content": f"turn {100 - orch._MAX_CONTEXT_MESSAGES}"}
    # session.messages itself is completely untouched — Phase 3's reconnect
    # and the live_conversation checkpoint both need the FULL history.
    assert len(session.messages) == 100


def test_trimmed_messages_never_starts_mid_tool_call_exchange() -> None:
    """A "tool" message with no preceding "assistant"-with-tool_calls in
    the SAME request is invalid and every OpenAI-compatible provider
    rejects it. The tool-call exchange has to have messages AFTER it too
    (not just before) for a naive last-N slice to actually land inside
    it — positioned here so the naive cut lands EXACTLY on the "tool"
    result message, the worst case."""
    session = _session()
    leading_filler = [{"role": "user", "content": f"leading {i}"} for i in range(10)]
    real_exchange = [
        {"role": "user", "content": "real question"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {}}]},
        {"role": "tool", "tool_call_id": "1", "content": "tool result"},
        {"role": "assistant", "content": "final reply"},
    ]
    trailing_filler = [
        {"role": "user", "content": f"trailing {i}"} for i in range(orch._MAX_CONTEXT_MESSAGES - 2)
    ]
    session.messages = leading_filler + real_exchange + trailing_filler
    # Confirm the setup actually creates the dangerous case before trusting
    # the assertions below: the naive last-N slice's first element really
    # is the orphaned "tool" message.
    naive_window = session.messages[-orch._MAX_CONTEXT_MESSAGES :]
    assert naive_window[0]["role"] == "tool"

    trimmed = orch._trimmed_messages(session)

    assert len(trimmed) <= orch._MAX_CONTEXT_MESSAGES
    assert trimmed[0]["role"] == "user"  # never starts on a "tool" or bare "assistant" message
    # No tool-result message anywhere without ITS OWN preceding tool-call
    # assistant message also present in the trimmed window.
    tool_call_ids_present = set()
    for msg in trimmed:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            tool_call_ids_present.update(tc["id"] for tc in msg["tool_calls"])
        if msg["role"] == "tool":
            assert msg["tool_call_id"] in tool_call_ids_present


def test_trimmed_messages_falls_back_to_full_history_if_no_user_boundary_exists() -> None:
    """A pathological case (no "user" turn anywhere in the naive window) —
    sending the untrimmed history is safer than a malformed sequence."""
    session = _session()
    session.messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": str(i), "type": "function", "function": {}}]}
        for i in range(orch._MAX_CONTEXT_MESSAGES + 5)
    ]

    trimmed = orch._trimmed_messages(session)

    assert trimmed == session.messages


def test_run_loop_actually_sends_a_trimmed_history_once_it_grows_past_the_limit() -> None:
    """Integration-level: the REAL messages list handed to complete_fn is
    bounded, not just the pure helper function in isolation."""
    session = _session()
    session.messages = [{"role": "user", "content": f"filler {i}"} for i in range(200)]
    seen: dict[str, list] = {}

    async def fake_complete(messages, tools):
        seen["messages"] = messages
        return CompletionResult(content="ok")

    asyncio.run(orch.run_turn(session, "the real question", fake_complete))

    # system prompt + at most _MAX_CONTEXT_MESSAGES of history (the new
    # user turn was just appended to session.messages before this call).
    assert len(seen["messages"]) <= orch._MAX_CONTEXT_MESSAGES + 1
    assert seen["messages"][0]["role"] == "system"
    # But the full 200+1 turns are still sitting in session.messages,
    # untouched, for reconnect/checkpoint purposes.
    assert len(session.messages) >= 201


# --- tool-call hardening (Phase 4, backend hardening plan) -------------------


def test_run_turn_missing_required_tool_argument_does_not_crash_the_turn() -> None:
    """submit_answer requires 'answer' — a model call that omits it must
    become a structured tool-error fed back to the model, never a raised
    TypeError that ends the whole turn/session."""
    session = _session()
    calls = {"n": 0}

    async def fake_complete(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return CompletionResult(
                tool_calls=[ToolCall(id="call_1", name="submit_answer", arguments={})]
            )
        return CompletionResult(content="Let me ask that differently.")

    result = asyncio.run(orch.run_turn(session, "I built a ledger service.", fake_complete))

    assert calls["n"] == 2  # the loop continued instead of crashing
    assert result.reply_text == "Let me ask that differently."
    assert session.ud.ctx.answers == []  # the malformed call never actually saved anything
    tool_messages = [m for m in session.messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "invalid arguments" in tool_messages[0]["content"]


def test_run_turn_unknown_tool_name_does_not_crash_the_turn() -> None:
    """A tool name the registry doesn't recognize (a model hallucination, or
    a schema drift) must be reported back, not raise KeyError/crash."""
    session = _session()
    calls = {"n": 0}

    async def fake_complete(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return CompletionResult(
                tool_calls=[ToolCall(id="call_1", name="delete_everything", arguments={})]
            )
        return CompletionResult(content="Sorry, let's continue.")

    result = asyncio.run(orch.run_turn(session, "an answer", fake_complete))

    assert calls["n"] == 2
    assert result.reply_text == "Sorry, let's continue."
    tool_messages = [m for m in session.messages if m["role"] == "tool"]
    assert tool_messages[0]["content"] == "Unknown tool: delete_everything"


def test_run_turn_malformed_tool_call_json_sentinel_does_not_crash_the_turn() -> None:
    """api/live.py substitutes a sentinel dict (shaped to mismatch every
    real tool signature) when the model's tool-call JSON fails to parse —
    this proves the orchestrator's OWN handling of that sentinel shape,
    independent of api/live.py's own JSON-decode test coverage."""
    session = _session()
    calls = {"n": 0}

    async def fake_complete(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="submit_answer",
                        arguments={"_malformed_tool_arguments": "{answer: not valid json"},
                    )
                ]
            )
        return CompletionResult(content="Could you say that again?")

    result = asyncio.run(orch.run_turn(session, "an answer", fake_complete))

    assert calls["n"] == 2
    assert result.reply_text == "Could you say that again?"
    assert session.ud.ctx.answers == []


def test_run_turn_bug_inside_a_tool_implementation_does_not_crash_the_turn() -> None:
    """A tool function that raises something other than TypeError (a genuine
    bug, or an unexpected internal failure) must ALSO be contained — the
    exact exception text must never reach the model's context."""
    session = _session()
    calls = {"n": 0}

    async def _broken_tool(session, **kwargs):
        raise ValueError("a secret internal detail that must not leak")

    import proven_hire_agent.live.orchestrator as orch_module

    original = orch_module.TOOL_REGISTRY.get("get_difficulty_hint")
    orch_module.TOOL_REGISTRY["get_difficulty_hint"] = _broken_tool
    try:

        async def fake_complete(messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return CompletionResult(
                    tool_calls=[ToolCall(id="call_1", name="get_difficulty_hint", arguments={})]
                )
            return CompletionResult(content="Moving on.")

        result = asyncio.run(orch.run_turn(session, "an answer", fake_complete))
    finally:
        assert original is not None
        orch_module.TOOL_REGISTRY["get_difficulty_hint"] = original

    assert calls["n"] == 2
    assert result.reply_text == "Moving on."
    tool_messages = [m for m in session.messages if m["role"] == "tool"]
    assert "secret internal detail" not in tool_messages[0]["content"]
    assert "internal error occurred" in tool_messages[0]["content"]


def test_run_loop_bounded_safety_valve_forces_a_real_reply_instead_of_silence() -> None:
    """A model that keeps calling tools forever must not hang the turn — the
    bounded loop stops after _MAX_TOOL_ROUNDS and forces ONE final
    completion with no tools offered, so the candidate gets an actual
    spoken reply (Phase 4) instead of the old silent empty return (which
    the candidate experienced as "I spoke, the server worked, then
    nothing")."""
    session = _session()

    async def fake_complete(messages, tools):
        if not tools:  # the forced final no-tools call
            return CompletionResult(content="Let's move on to the next question.")
        return CompletionResult(
            tool_calls=[ToolCall(id="call_x", name="get_difficulty_hint", arguments={})]
        )

    result = asyncio.run(orch.run_turn(session, "an answer", fake_complete))

    assert result.reply_text == "Let's move on to the next question."


def test_run_loop_bounded_safety_valve_falls_back_to_a_generic_line_if_the_forced_call_also_fails() -> None:
    """If even the forced final completion fails (every provider/retry
    exhausted), the candidate still gets SOME spoken line — never empty
    text and never a raised exception out of the turn loop."""
    session = _session()

    async def fake_complete(messages, tools):
        if not tools:  # the forced final no-tools call
            raise RuntimeError("provider unavailable")
        return CompletionResult(
            tool_calls=[ToolCall(id="call_x", name="get_difficulty_hint", arguments={})]
        )

    result = asyncio.run(orch.run_turn(session, "an answer", fake_complete))

    assert result.reply_text
    assert result.reply_text == orch._SAFETY_VALVE_FALLBACK_LINE


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


# --- transcript regression (Phase 1, backend hardening plan) -----------------


def test_run_turn_populates_transcript_for_answer_recovery() -> None:
    """Regression test: before this fix, InterviewUserdata.transcript was
    never populated on this transport, silently disabling reconstruct_answers,
    SessionGuard's turn-count ceiling, and transcript persistence. Scenario
    directly from the audit: the candidate gives a real answer, the model
    replies WITHOUT calling submit_answer, the connection then drops — the
    verbatim answer must still be recoverable from the transcript."""
    session = _session()
    q1 = session.ud.ctx.plan.questions[0]
    answer = (
        "I led the migration of our legacy monolith's billing module to a "
        "sharded ledger service, which cut reconciliation time from hours to minutes."
    )

    async def fake_complete(messages, tools):
        return CompletionResult(content="Thanks for sharing that.")

    result = asyncio.run(orch.run_turn(session, answer, fake_complete))

    # The tool was never called — ctx.answers/cursor are untouched...
    assert session.ud.ctx.answers == []
    assert session.ud.ctx.cursor == 0
    # ...but the transcript captured both real turns, the candidate's tagged
    # with the question that was actually current when they spoke.
    user_turns = [t for t in session.ud.transcript if t["role"] == "user"]
    assert len(user_turns) == 1
    assert user_turns[0]["text"] == answer
    assert user_turns[0]["question_id"] == q1.id
    assistant_turns = [t for t in session.ud.transcript if t["role"] == "assistant"]
    assert assistant_turns[-1]["text"] == result.reply_text

    # Simulating the disconnect: reconstruct_answers recovers exactly what
    # was said, associated with the right question.
    recovered = state.reconstruct_answers(session.ud)

    assert recovered == 1
    assert session.ud.ctx.answers[-1].question_id == q1.id
    assert session.ud.ctx.answers[-1].transcript == answer

    # A second shutdown-path invocation must not duplicate the recovered answer.
    recovered_again = state.reconstruct_answers(session.ud)
    assert recovered_again == 0
    assert len(session.ud.ctx.answers) == 1


def test_run_turn_transcript_tags_assistant_reply_with_new_question_after_advance() -> None:
    """When submit_answer advances the cursor mid-turn, the resulting
    assistant reply (the new question) is tagged with the NEW question's id,
    not the one the candidate was answering — matching how a real
    conversation transcript should read, and confirming add_turn's ordering:
    the candidate's turn is tagged BEFORE tool execution, the assistant's
    reply AFTER."""
    session = _session()
    q1 = session.ud.ctx.plan.questions[0]
    q2 = session.ud.ctx.plan.questions[1]
    calls = {"n": 0}

    async def fake_complete(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return CompletionResult(
                tool_calls=[
                    ToolCall(id="call_1", name="submit_answer", arguments={"answer": "Ledger service work."})
                ]
            )
        return CompletionResult(content="Great, tell me about your next project.")

    asyncio.run(orch.run_turn(session, "I built a ledger service.", fake_complete))

    user_turns = [t for t in session.ud.transcript if t["role"] == "user"]
    assistant_turns = [t for t in session.ud.transcript if t["role"] == "assistant"]
    assert user_turns[-1]["question_id"] == q1.id  # answered while Q1 was current
    assert assistant_turns[-1]["question_id"] == q2.id  # new question, now current


def test_run_loop_bounded_safety_valve_records_the_forced_reply_in_transcript() -> None:
    """The bounded-loop safety valve's forced final reply IS real spoken
    text (Phase 4) — it must be tagged into the transcript exactly like any
    other assistant turn, so answer-recovery/turn-count-ceiling stay
    accurate."""
    session = _session()

    async def fake_complete(messages, tools):
        if not tools:
            return CompletionResult(content="Let's move on to the next question.")
        return CompletionResult(
            tool_calls=[ToolCall(id="call_x", name="get_difficulty_hint", arguments={})]
        )

    asyncio.run(orch.run_turn(session, "an answer", fake_complete))

    assistant_turns = [t for t in session.ud.transcript if t["role"] == "assistant"]
    assert len(assistant_turns) == 1
    assert assistant_turns[0]["text"] == "Let's move on to the next question."
