"""FastAPI application factory for the Proven Hire Job Description Interview agent API.

Exposes a health check plus the prep and score routers. ``main()`` runs the app
under uvicorn on the configured port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from .api import coach as coach_api
from .api import integrity as integrity_api
from .api import kb as kb_api
from .api import live as live_api
from .api import prep as prep_api
from .api import proctoring as proctoring_api
from .api import score as score_api
from .api import session as session_api
from .api.auth import require_internal_secret
from .core.config import get_settings
from .core.logging import get_logger
from .core.observability import init_observability

log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Zero-config no-op unless SENTRY_DSN/LANGFUSE_* are set (see
    # core/observability.py) — this is the one call site that was missing;
    # the module itself was already fully built (Phase 8 of the backend
    # hardening plan).
    init_observability(get_settings())

    # Every in-process invariant added by the live-interview hardening work
    # (the connect-time asyncio.Lock in api/live.py, the in-process rate
    # limiter) depends on this process being the ONLY writer for whatever
    # session_ids it's serving. That's true today — Render runs this as a
    # single uvicorn worker, no --workers flag (see Dockerfile.api) — but
    # nothing in the code itself enforces or even checks it. Logged loudly at
    # startup so a future move to multiple workers/processes doesn't silently
    # reintroduce the exact races those mechanisms exist to close; it would
    # need real cross-process coordination (e.g. moving the connect-lock and
    # rate-limit state into Postgres/Redis), not just a deploy config change.
    log.info(
        "startup: in-process session-ownership and rate-limit guards assume "
        "a SINGLE process serves this agent-api. Do not scale to multiple "
        "workers/instances without redesigning those guards for cross-process "
        "coordination first."
    )
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Proven Hire Job Description Interview Agent API", lifespan=_lifespan)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    # Write/compute routers are gated by the optional internal secret (a no-op
    # unless INTERNAL_API_SECRET is set). The session router is included WITHOUT
    # the gate because it also serves the capability-guarded GET read path; its
    # live-result write carries the dependency on the route itself.
    guarded = [Depends(require_internal_secret)]
    app.include_router(prep_api.router, dependencies=guarded)
    app.include_router(score_api.router, dependencies=guarded)
    app.include_router(coach_api.router, dependencies=guarded)
    app.include_router(kb_api.router, dependencies=guarded)
    app.include_router(integrity_api.router, dependencies=guarded)
    app.include_router(proctoring_api.router, dependencies=guarded)
    app.include_router(live_api.router, dependencies=guarded)
    # The coordination WebSocket is deliberately UNGATED: a browser's native
    # WebSocket API can't set the X-Internal-Secret header, and Vercel can't
    # proxy a long-lived WS, so the browser connects to this route directly —
    # capability-guarded by the unguessable session_id, like session_api's
    # read route below.
    app.include_router(live_api.ws_router)
    app.include_router(session_api.router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=settings.agent_api_port)


if __name__ == "__main__":
    main()
