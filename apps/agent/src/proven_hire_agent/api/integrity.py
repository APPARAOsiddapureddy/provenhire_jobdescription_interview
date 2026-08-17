"""``/api/integrity-settings`` — the global proctoring/integrity-monitoring config.

One singleton row (see ``supabase/migrations/0007_integrity_settings.sql``,
``core/persistence/integrity_repository.py``). GET is read by both the
`/settings/integrity` admin page and the live interview room at session
start; PUT is the settings page's save action. Both are internal-secret
gated (see ``app.py``) — the web app is the only intended caller, proxying
through ``apps/web/app/api/integrity-settings/route.ts``.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..core.deps import build_deps
from ..shared_models import IntegritySettings

router = APIRouter()


@router.get("/api/integrity-settings", response_model=IntegritySettings)
async def get_integrity_settings() -> IntegritySettings:
    return await build_deps().integrity_repo.load()


@router.put("/api/integrity-settings", response_model=IntegritySettings)
async def put_integrity_settings(req: IntegritySettings) -> IntegritySettings:
    return await build_deps().integrity_repo.save(req)
