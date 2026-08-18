"""In-progress live-interview turn state (Live Voice Pipeline Replacement).

Mirrors ``repository.py``/``integrity_repository.py``'s shape: a Protocol +
a process-wide ``MemoryRepository`` singleton. Deliberately **no Supabase
variant** — unlike ``InterviewContext`` (the durable record, already
protected by the existing ``live-result`` checkpoint/``TranscriptFlusher``
pattern, ported unchanged from ``worker.py``), a :class:`LiveTurnSession`
holds transient WS-connection-lifetime state (the running chat history,
which persona is active). Losing it on a process restart is the same risk
profile a LiveKit worker crash already had — not made worse by this change.
"""

from __future__ import annotations

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

    async def delete(self, session_id: str) -> None: ...


class MemoryRepository:
    """In-memory repository. Process-wide singleton, dict keyed by session_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, LiveTurnSession] = {}

    async def get(self, session_id: str) -> LiveTurnSession | None:
        return self._sessions.get(session_id)

    async def put(self, session_id: str, session: LiveTurnSession) -> None:
        self._sessions[session_id] = session

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# Module-wide singleton so state survives across build_deps() calls.
_MEMORY_REPO = MemoryRepository()


def get_live_session_repository(settings: Settings) -> LiveSessionRepository:
    """Always the memory singleton — see the module docstring for why this
    domain deliberately has no Supabase-backed variant."""
    return _MEMORY_REPO
