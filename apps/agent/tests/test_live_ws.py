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

    def fake_make_complete_fn(settings):
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

    monkeypatch.setattr(live_api, "_make_openai_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws:
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

    def fake_make_complete_fn(settings):
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

    monkeypatch.setattr(live_api, "_make_openai_complete_fn", fake_make_complete_fn)
    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)

    client = TestClient(app)
    with client.websocket_connect(f"/api/live/session/{session_id}") as ws:
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
