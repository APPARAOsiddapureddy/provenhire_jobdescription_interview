"""In-progress live-interview turn state (Live Voice Pipeline Replacement).

Mirrors ``repository.py``/``integrity_repository.py``'s shape: a Protocol +
a process-wide ``MemoryRepository`` singleton. Deliberately **no Supabase
variant** — unlike ``InterviewContext`` (the durable record, already
protected by the existing ``live-result`` checkpoint/``TranscriptFlusher``
pattern, ported unchanged from ``worker.py``), a :class:`LiveTurnSession`
holds transient WS-connection-lifetime state (the running chat history,
which persona is active). Losing it on a process restart is the same risk
profile a LiveKit worker crash already had — not made worse by this change.

``try_claim`` (Phase 3 of the backend hardening plan) is the in-process half
of the session-ownership invariant: two WS connections for the same
session_id must never BOTH believe they're the live writer. Postgres's
optimistic-concurrency version column (Phase 2) protects the durable store,
but two connections can each hold their own in-memory ``LiveTurnSession`` and
each successfully win individual version-checked writes in sequence —
correct per-write, but still two independent conversations silently
diverging. ``try_claim`` closes that at the layer that's actually shared
in-process: a session_id already present here means another connection is
genuinely live right now (its shutdown sequence hasn't run), so the new
connection must be refused, never silently handed a second
``LiveTurnSession`` for the same id.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ...live.orchestrator import LiveTurnSession
from ..logging import get_logger

if TYPE_CHECKING:
    from ..config import Settings

log = get_logger(__name__)


@runtime_checkable
class LiveSessionRepository(Protocol):
    """Storage contract for in-progress live-turn state, keyed by session_id."""

    async def get(self, session_id: str) -> LiveTurnSession | None: ...

    async def put(self, session_id: str, session: LiveTurnSession) -> None: ...

    async def try_claim(self, session_id: str, session: LiveTurnSession) -> bool:
        """Atomically register ``session`` as the sole live owner of
        ``session_id``. Returns ``True`` if this caller is now the owner
        (nothing was registered for this id), ``False`` if another
        connection already owns it — the caller must refuse the new
        connection outright, never overwrite the existing entry.
        """
        ...

    async def delete(self, session_id: str) -> None: ...


class MemoryRepository:
    """In-memory repository. Process-wide singleton, dict keyed by session_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, LiveTurnSession] = {}
        # One lock per session_id ever seen, created on first use and kept
        # for the life of the process (never removed on delete) — see
        # try_claim's docstring for why a per-id lock is used at all rather
        # than relying on asyncio's single-threaded scheduling alone. The
        # dict grows by one small Lock object per unique session_id for the
        # process's lifetime; on a single Render worker with this product's
        # volume that's not a real concern, and removing entries on delete
        # would reopen the exact race a lock removed out from under a
        # concurrent claim attempt is meant to prevent.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def get(self, session_id: str) -> LiveTurnSession | None:
        return self._sessions.get(session_id)

    async def put(self, session_id: str, session: LiveTurnSession) -> None:
        self._sessions[session_id] = session

    async def try_claim(self, session_id: str, session: LiveTurnSession) -> bool:
        """The check-and-set here has no ``await`` in its body, so under
        asyncio's cooperative single-thread scheduling it is ALREADY atomic
        with respect to other coroutines — no two tasks can interleave
        mid-function without a yield point. The explicit lock exists anyway:
        it is the correct minimal primitive for this invariant (not a bare
        dict flag standing in for real protection), and it keeps the
        guarantee robust against a future change that adds an ``await``
        between the check and the set, rather than being correct only by
        accident of the current code shape.
        """
        async with self._lock_for(session_id):
            if session_id in self._sessions:
                return False
            self._sessions[session_id] = session
            return True

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# Module-wide singleton so state survives across build_deps() calls.
_MEMORY_REPO = MemoryRepository()


def get_live_session_repository(settings: Settings) -> LiveSessionRepository:
    """Always the memory singleton — see the module docstring for why this
    domain deliberately has no Supabase-backed variant."""
    return _MEMORY_REPO
