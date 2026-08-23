"""Offline tests for ``live/persistence.py``'s shutdown persist+score sequence.

Exercised directly against ``persist_and_score`` (not through the full WS
route — ``test_live_ws.py`` already covers that plumbing) so this stays on
ONE event loop via a single ``asyncio.run(...)``: TestClient runs the ASGI
app on its own background thread with its own event loop, and coordinating
a deliberately-slow background task across that boundary interacts badly
with asyncio's per-loop executor shutdown (a real hang was hit trying to
test this exact scenario through the WS route — this file exists because of
that, not just for style).
"""

from __future__ import annotations

import asyncio

from proven_hire_agent.core.adapters.mock import build_mock
from proven_hire_agent.core.deps import build_deps
from proven_hire_agent.live import persistence as live_persistence
from proven_hire_agent.live.state import InterviewUserdata
from proven_hire_agent.shared_models import AnswerRecord, InterviewContext, PrepRequest


def _run(coro):
    return asyncio.run(coro)


async def _fast_fail_persist_via_api(session_id, ud, settings, *, has_answers):
    """No real agent-api listening in tests — force the real fallback
    (persist_via_repo, in-process) instead of a real network call."""
    return False


def _prep_request() -> PrepRequest:
    from proven_hire_agent.shared_models import LanguageMode

    return PrepRequest(
        cv_url="https://example.com/cv.pdf",
        jd_text="We are hiring a backend engineer.",
        company="Acme Payments",
        language_mode=LanguageMode(primary="en", mixed=False),
    )


def _answered_userdata(session_id: str) -> InterviewUserdata:
    ctx = build_mock(InterviewContext)
    assert isinstance(ctx, InterviewContext)
    ud = InterviewUserdata(ctx=ctx, session_id=session_id)
    ud.ctx.answers = [
        AnswerRecord(question_id="q1", transcript="a real, substantive answer", started_at="", ended_at="")
    ]
    return ud


def test_persist_and_score_default_mode_awaits_scoring_inline(monkeypatch) -> None:
    """background_score=False (the default — worker.py's exact existing
    behavior, unchanged) must NOT return until scoring itself is done."""
    deps = build_deps()
    session_id = _run(deps.repo.create_session(_prep_request()))
    ud = _answered_userdata(session_id)

    scored = {"done": False}

    async def _fake_trigger_scoring(session_id, settings) -> None:
        await asyncio.sleep(0)  # yield once, same shape as a real await
        scored["done"] = True

    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _fake_trigger_scoring)

    _run(live_persistence.persist_and_score(session_id, ud, deps))

    assert scored["done"] is True


def test_persist_and_score_background_mode_returns_before_scoring_completes(monkeypatch) -> None:
    """Regression found via a real production smoke test: /api/score runs
    the full scoring LLM pipeline inline and can take minutes. The WS
    handler's shutdown sequence releases live_session_repo's ownership claim
    right after persist_and_score returns — so background_score=True MUST
    return as soon as persistence is durable, WITHOUT waiting for scoring,
    or a candidate who disconnects mid-interview and reconnects gets wrongly
    rejected with session_conflict, blocked behind their own still-running
    scoring job."""
    deps = build_deps()
    session_id = _run(deps.repo.create_session(_prep_request()))
    ud = _answered_userdata(session_id)

    scoring_started = asyncio.Event()
    scoring_may_finish = asyncio.Event()
    scored = {"done": False}

    async def _slow_trigger_scoring(session_id, settings) -> None:
        scoring_started.set()
        await scoring_may_finish.wait()
        scored["done"] = True

    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _slow_trigger_scoring)

    async def _scenario() -> None:
        # persist_and_score itself must return quickly — bounded wait_for
        # turns "silently hangs forever" into a fast, unambiguous failure.
        await asyncio.wait_for(
            live_persistence.persist_and_score(session_id, ud, deps, background_score=True),
            timeout=2.0,
        )
        # It returned WHILE scoring was still deliberately blocked, not
        # because scoring happened to finish fast.
        assert scoring_started.is_set()
        assert scored["done"] is False
        scoring_may_finish.set()
        await asyncio.sleep(0)  # let the background task actually finish
        assert scored["done"] is True

    _run(_scenario())


def test_persist_and_score_background_mode_still_persists_durably(monkeypatch) -> None:
    """background_score=True changes nothing about WHAT gets persisted —
    only whether scoring is awaited. The context/answers must land exactly
    like the default mode."""
    deps = build_deps()
    session_id = _run(deps.repo.create_session(_prep_request()))
    ud = _answered_userdata(session_id)

    async def _no_op_trigger_scoring(session_id, settings) -> None:
        return None

    monkeypatch.setattr(live_persistence, "persist_via_api", _fast_fail_persist_via_api)
    monkeypatch.setattr(live_persistence, "trigger_scoring", _no_op_trigger_scoring)

    _run(live_persistence.persist_and_score(session_id, ud, deps, background_score=True))

    view = _run(deps.repo.get_session_view(session_id))
    assert view is not None
    assert view.context is not None
    assert len(view.context.answers) == 1
    assert view.context.answers[0].transcript == "a real, substantive answer"
