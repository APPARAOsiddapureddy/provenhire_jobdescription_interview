"""Session routes: the session view + the worker's live-result write-back.

``GET /api/session/{id}`` returns a :class:`SessionView` (status, completed prep
steps, input-quality warnings, and the :class:`InterviewContext` once ready).
Unknown ids → 404.

``POST /api/session/{id}/live-result`` is the INTERNAL write path the voice
worker uses at shutdown. The worker runs in a separate process, so with no
Supabase configured its own in-memory repo is invisible to the API — answers
would be lost and never scored. Writing through this endpoint lands the result
in the store the API actually reads. (Like every route, it is session-id
capability-guarded: ids are unguessable uuid4.)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..core.deps import build_deps
from ..core.proctoring.scoring import compute_strike_score
from ..core.session_status import ALLOWED_LIVE_STATUSES, TERMINAL_STATUSES
from ..shared_models import InterviewContext, ProctoringEvent
from .auth import require_internal_secret
from .views import SessionView

router = APIRouter()


class LiveResultRequest(BaseModel):
    """Worker→API write-back: the post-interview context + verbatim transcript.

    API-internal (both ends are Python), so it lives here — NOT in
    ``shared_models`` — keeping the TS↔Pydantic parity registry untouched.
    """

    model_config = ConfigDict(extra="forbid")
    context: InterviewContext
    transcript: list[dict] = Field(default_factory=list)
    # Optional terminal hint ("no_answers" when the interview captured nothing).
    status: str | None = None
    # Optimistic-concurrency version the caller last read (Phase 2 of the
    # backend hardening plan). None — the LiveKit worker path's default, see
    # InterviewUserdata.version — skips the version check entirely,
    # preserving the old unconditional-overwrite behavior exactly; the WS
    # transport always supplies one.
    expected_version: int | None = None


@router.get("/api/session/{session_id}", response_model=SessionView)
async def get_session(session_id: str) -> SessionView:
    deps = build_deps()
    view = await deps.repo.get_session_view(session_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown session_id")
    # Compose the live proctoring score onto the read model rather than
    # storing it on the session row — see core/proctoring/scoring.py for why
    # this is a pure recompute-on-read, not a stored/decremented value.
    # Skipped once terminal: a finished interview's score is no longer live.
    if view.status not in TERMINAL_STATUSES:
        events = await deps.proctoring_repo.list_events(session_id)
        if events:
            score = compute_strike_score(events, now=datetime.now(timezone.utc))
            view = view.model_copy(update={"proctoring_score": score})
    return view


@router.get(
    "/api/session/{session_id}/proctoring-events",
    response_model=list[ProctoringEvent],
)
async def get_proctoring_events(session_id: str) -> list[ProctoringEvent]:
    """Full event list for a session — human review, not polled by the live room.

    Ungated like the session read route above: capability-guarded by the
    unguessable session_id, not the internal secret.
    """
    deps = build_deps()
    if await deps.repo.get_session_view(session_id) is None:
        raise HTTPException(status_code=404, detail="Unknown session_id")
    return await deps.proctoring_repo.list_events(session_id)


@router.post(
    "/api/session/{session_id}/live-result",
    dependencies=[Depends(require_internal_secret)],
)
async def post_live_result(session_id: str, req: LiveResultRequest) -> dict:
    deps = build_deps()
    view = await deps.repo.get_session_view(session_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown session_id")
    # Refuse to rewrite a session that has already reached a terminal state:
    # the transcript/answers are final once scored, and a replayed or forged
    # write must not be able to mutate them. This pre-check exists for a
    # clean, specific error message in the common case — the actual
    # atomicity guarantee is save_live_result's own conditional UPDATE below,
    # which re-verifies both this and the version at write time, closing the
    # real TOCTOU race a pre-check alone can't.
    if view.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"Session already {view.status}")
    if req.expected_version is not None and view.version != req.expected_version:
        raise HTTPException(
            status_code=409,
            detail=f"Stale version: request expected {req.expected_version}, session is at {view.version}",
        )

    new_version = await deps.repo.save_live_result(
        session_id,
        context=req.context,
        transcript=req.transcript or None,
        status=req.status if req.status in ALLOWED_LIVE_STATUSES else None,
        expected_version=req.expected_version,
    )
    if new_version is None:
        # Lost a race that landed between the pre-checks above and the
        # atomic write itself — a genuinely concurrent writer, not a bug.
        raise HTTPException(status_code=409, detail="Session changed concurrently")
    return {"ok": True, "version": new_version}
