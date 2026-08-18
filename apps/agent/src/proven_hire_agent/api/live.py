"""``/api/live`` — the custom (non-LiveKit) live-voice transport's backend seam.

Phase 1 of the LiveKit replacement (see the plan): mints a short-lived,
scoped Deepgram token for the browser's direct WebSocket connection, rather
than handing out the raw ``DEEPGRAM_API_KEY`` (the reference implementation
this was modeled on flagged that exact shortcut as a dev-only security gap
in its own code comments).

Internal-secret gated like every other write/compute route (see ``app.py``'s
``guarded`` list) — the web app is the only intended caller, proxying
through ``apps/web/app/api/live/deepgram-token/route.ts``.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ..core.deps import build_deps

router = APIRouter()

_DEEPGRAM_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"
# Deepgram's grant endpoint's own default/max TTL differs by plan; 60s is
# comfortably more than the ~8s the client allows itself to open the
# Deepgram WS (see InterviewSession's connect() timeout race) while staying
# short-lived. Re-verify against current Deepgram docs if grants start
# failing with a TTL-out-of-range error.
_TOKEN_TTL_SEC = 60


class DeepgramTokenResponse(BaseModel):
    """API-internal (both ends are this repo's own code) — not in
    shared_models, since this never needs TS<->Pydantic parity checking."""

    model_config = ConfigDict(extra="forbid")
    access_token: str
    expires_in: int


@router.post("/api/live/deepgram-token", response_model=DeepgramTokenResponse)
async def mint_deepgram_token() -> DeepgramTokenResponse:
    settings = build_deps().settings
    if not settings.deepgram_api_key:
        raise HTTPException(status_code=503, detail="Deepgram is not configured")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _DEEPGRAM_GRANT_URL,
            headers={"Authorization": f"Token {settings.deepgram_api_key}"},
            json={"ttl_seconds": _TOKEN_TTL_SEC},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Deepgram token grant failed: {resp.status_code} {resp.text[:200]}",
        )
    data = resp.json()
    return DeepgramTokenResponse(
        access_token=data["access_token"], expires_in=data.get("expires_in", _TOKEN_TTL_SEC)
    )
