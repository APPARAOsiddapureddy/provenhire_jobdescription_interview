"""FastAPI application factory for the Proven Hire Job Description Interview agent API.

Exposes a health check plus the prep and score routers. ``main()`` runs the app
under uvicorn on the configured port.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from .api import coach as coach_api
from .api import kb as kb_api
from .api import prep as prep_api
from .api import score as score_api
from .api import session as session_api
from .api.auth import require_internal_secret
from .core.config import get_settings


def create_app() -> FastAPI:
    app = FastAPI(title="Proven Hire Job Description Interview Agent API")

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
    app.include_router(session_api.router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=settings.agent_api_port)


if __name__ == "__main__":
    main()
