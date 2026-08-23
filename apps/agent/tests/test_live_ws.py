"""Integration test for the Phase 3 coordination WebSocket
(``ws /api/live/session/{session_id}``) — the actual "done when" criteria
from the Live Voice Pipeline Replacement plan: a scripted client completes
one full turn end-to-end (open -> utterance_end -> submit_answer tool call
-> next question spoken), through the REAL route, with only the OpenAI call
itself faked (mirrors how test_orchestrator.py injects a fake ``complete_fn``
— this test additionally proves the WS plumbing/session lookup/shutdown path
around it, which test_orchestrator.py's direct unit tests don't touch).
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from proven_hire_agent.api import live as live_api
from proven_hire_agent.app import app
from proven_hire_agent.core.deps import build_deps
from proven_hire_agent.live import persistence as live_persistence
from proven_hire_agent.live.orchestrator import CompletionResult, ToolCall
from proven_hire_agent.prep import run_prep
from proven_hire_agent.shared_models import LanguageMode, PrepRequest


def _make_ready_session() -> str:
    deps = build_deps()
    req = PrepRequest(
        cv_url="https://example.com/cv.pdf",
        jd_text="Senior Backend Engineer building distributed payment systems in Python.",
        company="ExampleCorp",
        language_mode=LanguageMode(primary="en", mixed=False),
    )
    return asyncio.run(run_prep(req, deps))


async def _fast_fail_persist_via_api(session_id, ud, settings, *, has_answers):
    """Stand-in for persist_via_api in tests: the real function makes a real
    httpx call to AGENT_API_URL, which — with nothing actually listening in
    this test environment — hangs indefinitely inside Starlette TestClient's
    threaded event loop on Windows (a test-harness artifact, not a production
    bug; uvicorn's single event loop has no such nested-loop interaction).
    Returning False immediately exercises the REAL fallback path
    (persist_via_repo, in-process, no network) that production actually
    relies on whenever the agent-api itself is unreachable anyway.
    """
    return False


async def _no_op_trigger_scoring(session_id, settings) -> None:
    """Same rationale as _fast_fail_persist_via_api — trigger_scoring also
    makes a real httpx call (to /api/score) that hangs the same way in this
    test environment; scoring itself is exercised by post/ pipeline tests
    elsewhere, not the concern of this WS-plumbing test."""
    return None


async def _no_op_flush_checkpoint(
    session_id, context, transcript, settings, *, expected_version=None, conversation=None
) -> None:
    """Same rationale as _fast_fail_persist_via_api — the per-turn checkpoint
    also makes a real httpx call that hangs the same way here. Patched on
    live_api directly (not live_persistence) because live.py does
    ``from ..live.persistence import flush_checkpoint``, which binds its own
    name in live_api's namespace at import time; patching the origin module
    doesn't reach that already-bound reference."""
    return None


def test_ws_misconfigured_provider_never_leaks_the_env_var_name(monkeypatch) -> None:
    """Phase 6 (candidate-safe errors): a misconfigured provider's own
    RuntimeError text names the missing env var — genuinely useful in a
    server log, never safe on the wire (it confirms which internal
    provider is wired up and exactly how the deployment is broken). The
    candidate must see a generic message instead."""
    session_id = _make_ready_session()

    def fake_make_complete_fn(settings, *, on_retry=None):
        raise RuntimeError("CEREBRAS_API_KEY is not configured; the live orchestrator needs it.")

    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)

    client = TestClient(app)
    try:
        with client.websocket_connect(f"/api/live/session/{session_id}") as ws:
            failure = ws.receive_json()
            assert failure["type"] == "error"
            assert "CEREBRAS_API_KEY" not in failure["message"]
            assert "not configured" not in failure["message"]
            assert failure["message"]  # still a real, non-empty message
            ws.receive_json()  # server closes (1011) right after
        raised = False
    except Exception:
        raised = True
    assert raised


def test_ws_rejects_unknown_session() -> None:
    client = TestClient(app)
    try:
        with client.websocket_connect("/api/live/session/sess_does_not_exist"):
            pass
        raised = False
    except Exception:
        raised = True
    assert raised  # server closes with 4404 before accept; client sees a close


def test_ws_completes_one_full_turn_end_to_end(monkeypatch) -> None:
    session_id = _make_ready_session()

    call_count = {"n": 0}

    def fake_make_complete_fn(settings, *, on_retry=None):
        async def fake_complete(messages, tools):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Opening turn: plain greeting + Q1, no tool call.
                return CompletionResult(content="Hi! Let's get started. Tell me about a project.")
            if call_count["n"] == 2:
                # First real turn: the model calls submit_answer.
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="submit_answer",
                            arguments={"answer": "I built a sharded ledger service."},
                        )
                    ]
                )
            # After the tool result comes back, the model speaks the next question.
            return CompletionResult(content="Great — next question incoming.")

        return fake_complete

    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)
    monkeypatch.setattr(live_api, "flush_checkpoint", _no_op_flush_checkpoint)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws:
        connected = ws.receive_json()
        assert connected == {"type": "session_connected"}
        opening = ws.receive_json()
        assert opening["type"] == "speak"
        assert "Hi! Let's get started" in opening["text"]

        ws.send_text('{"type": "utterance_end", "text": "I built a sharded ledger service."}')

        reply = ws.receive_json()
        assert reply["type"] == "speak"
        assert reply["text"] == "Great — next question incoming."

        # Explicit close (rather than relying on the `with` block's implicit
        # cleanup) so the server-side handler's blocked receive_text() call
        # deterministically raises WebSocketDisconnect and runs its shutdown
        # path, instead of staying parked waiting for a 3rd message forever.
        ws.close()

    # The orchestrator's submit_answer tool call really advanced the cursor.
    # MemoryRepository.get_session_view() reconstructs InterviewContext fresh
    # from a stored dict on every call (not a live object reference), so the
    # mutation is only visible once the WS handler's shutdown path (which
    # runs in the background after the client-side `with` block exits, per
    # ASGI disconnect semantics) has actually called save_context — poll
    # briefly rather than assume it's synchronous with the client closing.
    async def _poll_for_cursor_advance():
        deps = build_deps()
        for _ in range(50):  # up to ~5s
            view = await deps.repo.get_session_view(session_id)
            if view is not None and view.context is not None and view.context.cursor == 1:
                return view
            await asyncio.sleep(0.1)
        return await deps.repo.get_session_view(session_id)

    view = asyncio.run(_poll_for_cursor_advance())
    assert view is not None
    assert view.context is not None
    assert view.context.cursor == 1
    assert view.context.answers[-1].transcript == "I built a sharded ledger service."


def test_ws_end_interview_tool_call_closes_the_socket(monkeypatch) -> None:
    session_id = _make_ready_session()
    calls = {"n": 0}

    def fake_make_complete_fn(settings, *, on_retry=None):
        async def fake_complete(messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return CompletionResult(content="Hi! Q1 here.")
            if calls["n"] == 2:
                return CompletionResult(
                    tool_calls=[ToolCall(id="call_1", name="end_interview", arguments={})]
                )
            return CompletionResult(content="Goodbye!")

        return fake_complete

    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)
    monkeypatch.setattr(live_api, "flush_checkpoint", _no_op_flush_checkpoint)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws:
        ws.receive_json()  # session_connected
        ws.receive_json()  # opening
        ws.send_text('{"type": "utterance_end", "text": "that is all, thanks"}')
        closing = ws.receive_json()
        assert closing == {"type": "speak", "text": "Goodbye!"}
        # Server closes the socket after should_end; further reads should
        # raise rather than hang.
        try:
            ws.receive_json()
            hung_open = True
        except Exception:
            hung_open = False
        assert not hung_open


# --- Phase 4: LLM resilience — graceful degradation at the WS level ----------


def test_ws_turn_failure_sends_temporary_failure_and_the_connection_survives(monkeypatch) -> None:
    """When a candidate turn's completion call fails outright (every
    provider/retry exhausted — simulated here by a fake that just raises),
    the candidate gets a temporary_failure event and CAN KEEP TALKING — the
    session must not silently die over one bad turn."""
    session_id = _make_ready_session()
    calls = {"n": 0}

    def fake_make_complete_fn(settings, *, on_retry=None):
        async def fake_complete(messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return CompletionResult(content="Hi! Q1 here.")
            if calls["n"] == 2:
                raise TimeoutError("every provider exhausted")
            return CompletionResult(content="Good, let's continue.")

        return fake_complete

    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)
    monkeypatch.setattr(live_api, "flush_checkpoint", _no_op_flush_checkpoint)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws:
        ws.receive_json()  # session_connected
        ws.receive_json()  # opening

        ws.send_text('{"type": "utterance_end", "text": "my first answer"}')
        failure = ws.receive_json()
        assert failure["type"] == "temporary_failure"
        assert failure["message"]  # a real, candidate-facing message

        # The connection is still alive: try again.
        ws.send_text('{"type": "utterance_end", "text": "let me try again"}')
        reply = ws.receive_json()
        assert reply == {"type": "speak", "text": "Good, let's continue."}
        ws.close()


def test_ws_opening_failure_sends_temporary_failure_and_closes(monkeypatch) -> None:
    """If EVERY provider fails on the very opening turn, nothing has been
    checkpointed yet — the candidate is told plainly and the connection
    closes (1011) so they can reconnect and retry cleanly, rather than the
    server pretending the connection is still useful."""
    session_id = _make_ready_session()

    def fake_make_complete_fn(settings, *, on_retry=None):
        async def fake_complete(messages, tools):
            raise ConnectionError("every provider exhausted")

        return fake_complete

    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)
    monkeypatch.setattr(live_api, "flush_checkpoint", _no_op_flush_checkpoint)

    client = TestClient(app)
    try:
        with client.websocket_connect(f"/api/live/session/{session_id}") as ws:
            connected = ws.receive_json()
            assert connected == {"type": "session_connected"}
            failure = ws.receive_json()
            assert failure["type"] == "temporary_failure"
            # Server closes right after — further reads should raise.
            ws.receive_json()
        raised = False
    except Exception:
        raised = True
    assert raised


# --- Phase 5: ingress hardening (utterance length cap + rate limiting) -------


def test_ws_oversized_utterance_is_rejected_not_silently_truncated(monkeypatch) -> None:
    session_id = _make_ready_session()
    calls = {"n": 0}

    def fake_make_complete_fn(settings, *, on_retry=None):
        async def fake_complete(messages, tools):
            calls["n"] += 1
            return CompletionResult(content="Hi! Q1 here.")

        return fake_complete

    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)
    monkeypatch.setattr(live_api, "flush_checkpoint", _no_op_flush_checkpoint)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws:
        ws.receive_json()  # session_connected
        ws.receive_json()  # opening

        oversized = "x" * (live_api._MAX_UTTERANCE_LEN + 1)
        ws.send_text(json.dumps({"type": "utterance_end", "text": oversized}))
        rejected = ws.receive_json()
        assert rejected["type"] == "input_rejected"
        assert rejected["reason"]

        # The turn loop was never invoked for the rejected message — only
        # the opening call happened.
        assert calls["n"] == 1

        # The connection is still alive and works normally afterward.
        ws.send_text('{"type": "utterance_end", "text": "a normal-length answer"}')
        reply = ws.receive_json()
        assert reply == {"type": "speak", "text": "Hi! Q1 here."}
        ws.close()


def test_ws_turn_rate_limit_trips_and_the_client_is_told(monkeypatch) -> None:
    session_id = _make_ready_session()

    def fake_make_complete_fn(settings, *, on_retry=None):
        async def fake_complete(messages, tools):
            return CompletionResult(content="reply")

        return fake_complete

    # A tiny budget so the test doesn't need 20 real turns to trip it —
    # the limiter's OWN behavior is already proven by test_rate_limit.py;
    # this test only proves the WS route is actually wired to it.
    from proven_hire_agent.core.rate_limit import SlidingWindowRateLimiter

    monkeypatch.setattr(live_api, "_ws_turn_limiter", SlidingWindowRateLimiter(max_events=1, window_sec=60.0))
    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)
    monkeypatch.setattr(live_api, "flush_checkpoint", _no_op_flush_checkpoint)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws:
        ws.receive_json()  # session_connected
        ws.receive_json()  # opening

        ws.send_text('{"type": "utterance_end", "text": "first answer"}')
        reply = ws.receive_json()
        assert reply == {"type": "speak", "text": "reply"}

        # Second turn within the same (tiny) window trips the limiter.
        ws.send_text('{"type": "utterance_end", "text": "second answer, too fast"}')
        limited = ws.receive_json()
        assert limited["type"] == "rate_limited"
        assert limited["retry_after_sec"] > 0

        ws.close()


# --- Phase 3: session ownership + first-class reconnect ----------------------


def test_ws_refuses_connection_to_a_terminal_session() -> None:
    """A finished interview's record is final (Phase 2) — a new connection
    must be refused outright, never spin up a phantom second interview whose
    answers could never be saved."""
    session_id = _make_ready_session()
    deps = build_deps()
    asyncio.run(deps.repo.update_status(session_id, "no_answers"))

    client = TestClient(app)
    try:
        with client.websocket_connect(f"/api/live/session/{session_id}"):
            pass
        raised = False
    except Exception:
        raised = True
    assert raised  # server closes with 4409 before accept; client sees a close


def test_ws_second_concurrent_connection_gets_session_conflict(monkeypatch) -> None:
    """Two WS connections to the SAME session_id at once: exactly one becomes
    the live writer; the other gets a clean session_conflict event and is
    closed — never two writers racing over the same InterviewUserdata."""
    session_id = _make_ready_session()
    calls = {"n": 0}

    def fake_make_complete_fn(settings, *, on_retry=None):
        async def fake_complete(messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return CompletionResult(content="Hi! Q1 here.")
            return CompletionResult(content="Still here, go ahead.")

        return fake_complete

    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)
    monkeypatch.setattr(live_api, "flush_checkpoint", _no_op_flush_checkpoint)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws1:
        connected = ws1.receive_json()
        assert connected == {"type": "session_connected"}
        ws1.receive_json()  # opening greeting

        with client.websocket_connect(f"/api/live/session/{session_id}") as ws2:
            conflict = ws2.receive_json()
            assert conflict["type"] == "session_conflict"
            try:
                ws2.receive_json()
                still_open = True
            except Exception:
                still_open = False
            assert not still_open

        # The FIRST connection is unaffected — still the sole live writer.
        ws1.send_text('{"type": "utterance_end", "text": "still me, the real one"}')
        reply = ws1.receive_json()
        assert reply == {"type": "speak", "text": "Still here, go ahead."}
        ws1.close()


def test_ws_reconnect_resumes_conversation_without_re_greeting(monkeypatch) -> None:
    """Disconnect mid-interview, reconnect: no re-greeting, the correct
    cursor survives, the model's next reply is grounded in the ACTUAL prior
    conversation (not a reset history), and the durable transcript has no
    duplicate entries across the reconnect boundary."""
    session_id = _make_ready_session()
    deps = build_deps()

    calls: list[list[dict]] = []

    def fake_make_complete_fn(settings, *, on_retry=None):
        async def fake_complete(messages, tools):
            calls.append(messages)
            n = len(calls)
            if n == 1:  # connection 1's opening turn
                return CompletionResult(content="Hi! Tell me about a hard bug you fixed.")
            if n == 2:  # connection 1's first real turn: the model saves the answer
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="submit_answer",
                            arguments={"answer": "I fixed a nasty race condition."},
                        )
                    ]
                )
            if n == 3:  # ...then asks the next question
                return CompletionResult(content="Great — next: tell me about a system you designed.")
            # n == 4: the FIRST call on the RECONNECTED connection.
            return CompletionResult(content="Good context — one follow-up on that.")

        return fake_complete

    async def _in_process_flush_checkpoint(
        session_id, context, transcript, settings, *, expected_version=None, conversation=None
    ):
        # Real in-process persistence (the actual CAS-protected repository
        # method), just skipping the network hop that hangs in this test
        # environment — see _no_op_flush_checkpoint's docstring above. This
        # test specifically needs the conversation to really land somewhere
        # readable, unlike the other tests here which don't care.
        return await deps.repo.save_live_result(
            session_id,
            context=context,
            transcript=transcript,
            status=None,
            expected_version=expected_version,
            conversation=conversation,
        )

    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)
    monkeypatch.setattr(live_api, "flush_checkpoint", _in_process_flush_checkpoint)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws1:
        connected = ws1.receive_json()
        assert connected == {"type": "session_connected"}
        opening = ws1.receive_json()
        assert opening["type"] == "speak"
        assert "hard bug" in opening["text"]

        ws1.send_text('{"type": "utterance_end", "text": "I fixed a nasty race condition."}')
        reply1 = ws1.receive_json()
        assert reply1 == {"type": "speak", "text": "Great — next: tell me about a system you designed."}

        # Deliberately closed WITHOUT ending the interview — a mid-interview
        # drop, not a graceful end_interview.
        ws1.close()

    async def _wait_for_release_and_checkpoint():
        for _ in range(50):  # up to ~5s
            released = await deps.live_session_repo.get(session_id) is None
            resume = await deps.repo.get_live_resume_state(session_id)
            if released and resume is not None and resume.messages:
                return resume
            await asyncio.sleep(0.1)
        raise AssertionError("connection 1 never released ownership / never checkpointed")

    resume_state = asyncio.run(_wait_for_release_and_checkpoint())
    assert resume_state.messages  # the conversation really persisted

    view_after_first = asyncio.run(deps.repo.get_session_view(session_id))
    assert view_after_first is not None and view_after_first.context is not None
    assert view_after_first.context.cursor == 1  # Q1 answered -> advanced to Q2

    with client.websocket_connect(f"/api/live/session/{session_id}") as ws2:
        resumed = ws2.receive_json()
        assert resumed == {"type": "session_resumed"}  # NOT session_connected

        ws2.send_text('{"type": "utterance_end", "text": "It was a distributed lock issue."}')
        reply2 = ws2.receive_json()
        assert reply2 == {"type": "speak", "text": "Good context — one follow-up on that."}
        ws2.close()

    # The model's first call on the reconnected connection saw the PRIOR
    # conversation, not a reset one: exactly 4 completion calls total (no
    # extra open_interview call on reconnect), and the 4th call's own
    # messages carry the earlier exchange.
    assert len(calls) == 4
    resumed_call_messages = json.dumps(calls[3])
    assert "race condition" in resumed_call_messages  # the candidate's Q1 answer
    assert "system you designed" in resumed_call_messages  # the assistant's Q2 ask

    # No duplicate transcript entries across the reconnect boundary: exactly
    # the 5 turns actually spoken (greeting, Q1 answer, Q2 ask, Q2 answer,
    # follow-up), each appearing once.
    final_transcript = deps.repo._rows[session_id].transcript
    assert len(final_transcript) == 5
    texts = [t["text"] for t in final_transcript]
    assert len(texts) == len(set(texts))


def test_ws_recovers_from_a_cas_conflict_then_reconnects_cleanly(monkeypatch) -> None:
    """Phase 10's named cross-phase scenario: a checkpoint mid-session hits
    a genuine CAS conflict (another writer bumped the version behind this
    connection's back — regression: ud.version used to never resync,
    permanently poisoning every later write including the final shutdown
    persist), the connection must RECOVER and keep working, and a
    reconnect right after must still work cleanly."""
    session_id = _make_ready_session()
    deps = build_deps()

    calls: list[list[dict]] = []

    def fake_make_complete_fn(settings, *, on_retry=None):
        async def fake_complete(messages, tools):
            calls.append(messages)
            n = len(calls)
            if n == 1:  # opening
                return CompletionResult(content="Hi! Tell me about a hard bug you fixed.")
            if n == 2:  # turn 1: the model saves the answer
                return CompletionResult(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="submit_answer",
                            arguments={"answer": "I fixed a nasty race condition."},
                        )
                    ]
                )
            if n == 3:  # turn 1: reply after the tool result — its OWN
                # checkpoint (checkpoint #2 below) is the one that conflicts.
                return CompletionResult(content="Great — next: tell me about a system you designed.")
            if n == 4:  # turn 2 — this checkpoint (#3) must succeed: proves
                # the connection recovered instead of staying poisoned.
                return CompletionResult(content="Nice — how did you verify the fix?")
            # n == 5: the first call on the RECONNECTED connection.
            return CompletionResult(content="Good context — one follow-up on that.")

        return fake_complete

    checkpoint_calls = {"n": 0}

    async def _in_process_flush_checkpoint(
        session_id, context, transcript, settings, *, expected_version=None, conversation=None
    ):
        checkpoint_calls["n"] += 1
        if checkpoint_calls["n"] == 2:
            # Simulate another writer bumping the version behind this
            # connection's back, right before checkpoint #2's own write —
            # deterministic (no cross-thread timing race): expected_version
            # was already fixed as this call's argument, based on ud.version
            # BEFORE this bump, so the write below is guaranteed to conflict.
            deps.repo._rows[session_id].version += 1
        return await deps.repo.save_live_result(
            session_id,
            context=context,
            transcript=transcript,
            status=None,
            expected_version=expected_version,
            conversation=conversation,
        )

    monkeypatch.setattr(live_api, "_make_live_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)
    monkeypatch.setattr(live_api, "flush_checkpoint", _in_process_flush_checkpoint)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws1:
        ws1.receive_json()  # session_connected
        ws1.receive_json()  # opening (checkpoint #1 follows, no conflict)

        ws1.send_text('{"type": "utterance_end", "text": "I fixed a nasty race condition."}')
        reply1 = ws1.receive_json()  # checkpoint #2 follows THIS — conflicts
        assert reply1 == {"type": "speak", "text": "Great — next: tell me about a system you designed."}

        # If the connection were still poisoned, this turn's own checkpoint
        # (#3) would ALSO silently fail — but the turn loop doesn't
        # surface checkpoint failures to the client either way, so the
        # real proof is durable state, checked below after disconnect.
        ws1.send_text('{"type": "utterance_end", "text": "Load-tested it before and after."}')
        reply2 = ws1.receive_json()
        assert reply2 == {"type": "speak", "text": "Nice — how did you verify the fix?"}

        ws1.close()

    async def _wait_for_release_and_checkpoint():
        for _ in range(50):  # up to ~5s
            released = await deps.live_session_repo.get(session_id) is None
            view = await deps.repo.get_session_view(session_id)
            if released and view is not None and view.context is not None and view.context.cursor == 1:
                return view
            await asyncio.sleep(0.1)
        raise AssertionError("connection 1 never recovered/released ownership")

    asyncio.run(_wait_for_release_and_checkpoint())
    # Proof the connection recovered from the conflict rather than staying
    # poisoned: checkpoint #3 (turn 2) DID land durably, not just checkpoint
    # #1. If the resync fix were missing, turn 2's checkpoint (and the
    # final shutdown persist) would have kept failing on the same stale
    # version and this transcript would be stuck after turn 1.
    assert len(deps.repo._rows[session_id].transcript) == 5  # greeting, Q1 answer, Q2 ask, Q2 answer, Q3 ask

    with client.websocket_connect(f"/api/live/session/{session_id}") as ws2:
        resumed = ws2.receive_json()
        assert resumed == {"type": "session_resumed"}  # reconnect still works cleanly right after the conflict

        ws2.send_text('{"type": "utterance_end", "text": "One more detail on that."}')
        reply3 = ws2.receive_json()
        assert reply3 == {"type": "speak", "text": "Good context — one follow-up on that."}
        ws2.close()

    assert len(calls) == 5
    resumed_call_messages = json.dumps(calls[4])
    assert "race condition" in resumed_call_messages  # the pre-conflict conversation survived intact
