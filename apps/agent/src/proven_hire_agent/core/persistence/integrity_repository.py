"""Integrity/proctoring settings persistence.

Mirrors ``repository.py``'s shape exactly: a :class:`IntegritySettingsRepository`
protocol, a process-wide :class:`MemoryRepository` singleton (all-OFF default,
offline-safe), and a :class:`SupabaseRepository` that persists the single
``public.integrity_settings`` row (see
``supabase/migrations/0007_integrity_settings.sql``) via a lazy ``supabase``
import. Unlike sessions, there is exactly one row — no session_id keying.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ...shared_models import IntegritySettings
from ..logging import get_logger

if TYPE_CHECKING:
    from ..config import Settings

log = get_logger(__name__)

_DEFAULTS: dict[str, str] = {
    "ai_behavior_analysis": "off",
    "camera_required": "off",
    "copy_paste_detection": "off",
    "devtools_detection": "off",
    "fullscreen_required": "off",
    "microphone_monitoring": "off",
    "camera_ai_detection": "off",
    "three_strike_auto_end": "off",
    "screen_recording_enabled": "off",
    "tab_switching_detection": "off",
}


@runtime_checkable
class IntegritySettingsRepository(Protocol):
    """Storage contract for the global integrity/proctoring settings row."""

    async def load(self) -> IntegritySettings: ...

    async def save(self, settings: IntegritySettings) -> IntegritySettings: ...


class MemoryRepository:
    """In-memory repository. Starts all-OFF; process-wide singleton."""

    def __init__(self) -> None:
        self._row: IntegritySettings = IntegritySettings(**_DEFAULTS)

    async def load(self) -> IntegritySettings:
        return self._row

    async def save(self, settings: IntegritySettings) -> IntegritySettings:
        self._row = settings
        return self._row


class SupabaseRepository:
    """Persist the singleton row to Supabase ``public.integrity_settings``."""

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
        return self._client.table("integrity_settings")

    async def _exec(self, build: Any) -> Any:
        import asyncio

        return await asyncio.to_thread(build)

    async def load(self) -> IntegritySettings:
        def _build() -> Any:
            return self._table().select("*").eq("id", 1).limit(1).execute()

        resp = await self._exec(_build)
        rows = getattr(resp, "data", None) or []
        if not rows:
            # Migration seeds row id=1, but degrade to defaults rather than
            # raising if it's somehow missing (e.g. migration not yet applied).
            log.warning("integrity_settings row missing; using all-OFF defaults")
            return IntegritySettings(**_DEFAULTS)
        row = dict(rows[0])
        row.pop("id", None)
        return IntegritySettings.model_validate(row)

    async def save(self, settings: IntegritySettings) -> IntegritySettings:
        payload = settings.model_dump(exclude={"updated_at"})

        def _build() -> Any:
            return self._table().update(payload).eq("id", 1).execute()

        await self._exec(_build)
        return await self.load()


# Module-wide singleton so MemoryRepository state survives across build_deps() calls.
_MEMORY_REPO = MemoryRepository()


def get_integrity_repository(settings: Settings) -> IntegritySettingsRepository:
    """Return a repository: Supabase if fully configured, else the memory singleton."""
    if settings.supabase_url and settings.supabase_service_role_key:
        return SupabaseRepository(settings.supabase_url, settings.supabase_service_role_key)
    if settings.supabase_url or settings.supabase_service_role_key:
        log.error(
            "Supabase is PARTIALLY configured (need BOTH SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY); falling back to the in-memory "
            "integrity-settings store — changes will NOT survive a restart."
        )
    return _MEMORY_REPO
