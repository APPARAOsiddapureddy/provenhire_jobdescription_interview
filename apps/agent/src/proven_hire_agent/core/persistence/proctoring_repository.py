"""Generic proctoring event log persistence.

Mirrors ``repository.py``/``integrity_repository.py``'s shape exactly: a
:class:`ProctoringEventsRepository` protocol, a process-wide
:class:`MemoryRepository` singleton, and a :class:`SupabaseRepository` that
persists rows to ``public.proctoring_events`` (see ``supabase/migrations/
0008_proctoring_events.sql``) via a lazy ``supabase`` import. Unlike
``integrity_settings`` (one singleton row), this is keyed per-``session_id``
— append-only event rows, not a single upsertable config row.

Deliberately holds NO scoring/decay logic — see ``core/proctoring/scoring.py``
for why that lives in a pure function instead of here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ...shared_models import ProctoringEvent, ProctoringEventCreate
from ..logging import get_logger

if TYPE_CHECKING:
    from ..config import Settings

log = get_logger(__name__)


@runtime_checkable
class ProctoringEventsRepository(Protocol):
    """Storage contract for the generic proctoring event log."""

    async def record_event(self, event: ProctoringEventCreate) -> ProctoringEvent: ...

    async def list_events(self, session_id: str) -> list[ProctoringEvent]: ...


class MemoryRepository:
    """In-memory repository. Process-wide singleton, dict keyed by session_id."""

    def __init__(self) -> None:
        self._events: dict[str, list[ProctoringEvent]] = {}

    async def record_event(self, event: ProctoringEventCreate) -> ProctoringEvent:
        row = ProctoringEvent(
            id=str(uuid.uuid4()),
            created_at=datetime.now(UTC).isoformat(),
            **event.model_dump(),
        )
        self._events.setdefault(event.session_id, []).append(row)
        return row

    async def list_events(self, session_id: str) -> list[ProctoringEvent]:
        return list(self._events.get(session_id, []))


class SupabaseRepository:
    """Persist proctoring events to Supabase ``public.proctoring_events``."""

    def __init__(self, url: str, service_role_key: str) -> None:
        self._url = url
        self._key = service_role_key
        self._client: Any | None = None

    def _table(self) -> Any:
        if self._client is None:
            try:
                from supabase import create_client
            except ImportError as exc:  # pragma: no cover - depends on optional SDK
                raise RuntimeError(
                    "supabase is not installed; install the 'supabase' extra."
                ) from exc
            self._client = create_client(self._url, self._key)
        return self._client.table("proctoring_events")

    async def _exec(self, build: Any) -> Any:
        import asyncio

        return await asyncio.to_thread(build)

    async def record_event(self, event: ProctoringEventCreate) -> ProctoringEvent:
        payload = event.model_dump()

        def _build() -> Any:
            return self._table().insert(payload).execute()

        resp = await self._exec(_build)
        rows = getattr(resp, "data", None) or []
        if not rows:
            raise RuntimeError("proctoring_events insert returned no row")
        return ProctoringEvent.model_validate(rows[0])

    async def list_events(self, session_id: str) -> list[ProctoringEvent]:
        def _build() -> Any:
            return (
                self._table()
                .select("*")
                .eq("session_id", session_id)
                .order("created_at")
                .execute()
            )

        resp = await self._exec(_build)
        rows = getattr(resp, "data", None) or []
        return [ProctoringEvent.model_validate(row) for row in rows]


# Module-wide singleton so MemoryRepository state survives across build_deps() calls.
_MEMORY_REPO = MemoryRepository()


def get_proctoring_repository(settings: Settings) -> ProctoringEventsRepository:
    """Return a repository: Supabase if fully configured, else the memory singleton."""
    if settings.supabase_url and settings.supabase_service_role_key:
        return SupabaseRepository(settings.supabase_url, settings.supabase_service_role_key)
    if settings.supabase_url or settings.supabase_service_role_key:
        log.error(
            "Supabase is PARTIALLY configured (need BOTH SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY); falling back to the in-memory "
            "proctoring-events store — events will NOT survive a restart."
        )
    return _MEMORY_REPO
