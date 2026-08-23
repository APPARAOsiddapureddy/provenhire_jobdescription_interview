"""Offline tests for the in-process live-session ownership primitive
(Phase 3a of the live-interview backend hardening plan).

``try_claim`` is the in-process half of "two WS connections for one
session_id must never both believe they're the live writer" — Postgres's
version column (Phase 2) protects the durable store, but this is the only
mechanism that can enforce it at the layer that's actually shared
in-process. Exercised directly against ``MemoryRepository`` (the only
implementation — see the module docstring for why there's no Supabase
variant) rather than through the WS route, mirroring how ``test_guard.py``
unit-tests ``SessionGuard`` directly before ``test_live_ws.py`` proves it
end-to-end.
"""

from __future__ import annotations

import asyncio

from proven_hire_agent.core.adapters.mock import build_mock
from proven_hire_agent.core.persistence.live_session_repository import MemoryRepository
from proven_hire_agent.live.orchestrator import LiveTurnSession
from proven_hire_agent.live.state import InterviewUserdata
from proven_hire_agent.shared_models import InterviewContext


def _run(coro):
    return asyncio.run(coro)


def _session(session_id: str) -> LiveTurnSession:
    ctx = build_mock(InterviewContext)
    assert isinstance(ctx, InterviewContext)
    ud = InterviewUserdata(ctx=ctx, session_id=session_id)
    return LiveTurnSession(ud=ud)


def test_try_claim_succeeds_for_a_session_id_with_no_existing_owner() -> None:
    repo = MemoryRepository()
    claimed = _run(repo.try_claim("sess_1", _session("sess_1")))
    assert claimed is True
    assert _run(repo.get("sess_1")) is not None


def test_try_claim_rejects_a_second_claim_while_the_first_is_still_live() -> None:
    repo = MemoryRepository()
    first = _session("sess_1")
    second = _session("sess_1")

    assert _run(repo.try_claim("sess_1", first)) is True
    assert _run(repo.try_claim("sess_1", second)) is False

    # The rejected claim must NOT have overwritten the winner's session.
    assert _run(repo.get("sess_1")) is first


def test_try_claim_succeeds_again_after_the_owner_releases_it() -> None:
    repo = MemoryRepository()
    _run(repo.try_claim("sess_1", _session("sess_1")))
    _run(repo.delete("sess_1"))

    later = _session("sess_1")
    assert _run(repo.try_claim("sess_1", later)) is True
    assert _run(repo.get("sess_1")) is later


def test_try_claim_is_independent_per_session_id() -> None:
    repo = MemoryRepository()
    assert _run(repo.try_claim("sess_1", _session("sess_1"))) is True
    assert _run(repo.try_claim("sess_2", _session("sess_2"))) is True
    assert _run(repo.get("sess_1")) is not None
    assert _run(repo.get("sess_2")) is not None


def test_concurrent_try_claim_calls_for_the_same_id_yield_exactly_one_winner() -> None:
    """The real-world shape the lock protects: two coroutines racing to
    claim the SAME session_id concurrently (asyncio.gather, not sequential
    calls) — exactly one must win, regardless of scheduling order."""
    repo = MemoryRepository()
    contenders = [_session("sess_1") for _ in range(8)]

    async def _race() -> list[bool]:
        return await asyncio.gather(*(repo.try_claim("sess_1", s) for s in contenders))

    results = _run(_race())
    assert results.count(True) == 1
    assert results.count(False) == len(contenders) - 1
