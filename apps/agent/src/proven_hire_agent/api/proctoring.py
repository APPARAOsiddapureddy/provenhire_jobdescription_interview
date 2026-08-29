"""``POST /api/proctoring-events`` — the one generic proctoring write endpoint.

``type`` is free-form text (see ``shared_models.ProctoringEventCreate``) — a
new detector is just "pick a new string," no migration or enum change
needed. Records the event, recomputes the session's weighted-decay strike
score, and returns both in one round trip so the caller learns immediately
whether this event tipped the interview over the ban threshold. Internal-
secret gated (see ``app.py``) — the web app is the only intended caller,
proxying through ``apps/web/app/api/proctoring-events/route.ts``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from ..core.deps import build_deps
from ..core.proctoring.scoring import compute_strike_score, is_ban_threshold_exceeded
from ..shared_models import ProctoringEventCreate, ProctoringEventResponse

router = APIRouter()

# Enforced here rather than as max_length on the shared contract model so the
# generated JSON Schemas stay in TS<->Pydantic parity (matches prep.py's
# _MAX_CV_URL_LEN convention). photo is a downscaled canvas snapshot, not a
# CV upload — a few hundred KB is generous.
_MAX_TYPE_LEN = 64
_MAX_MESSAGE_LEN = 2_000
_MAX_PHOTO_LEN = 300_000


@router.post("/api/proctoring-events", response_model=ProctoringEventResponse)
async def post_proctoring_event(req: ProctoringEventCreate) -> ProctoringEventResponse:
    if (
        len(req.type) > _MAX_TYPE_LEN
        or len(req.message) > _MAX_MESSAGE_LEN
        or (req.photo is not None and len(req.photo) > _MAX_PHOTO_LEN)
    ):
        raise HTTPException(status_code=413, detail="Proctoring event payload too large")

    deps = build_deps()
    view = await deps.repo.get_session_view(req.session_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    event = await deps.proctoring_repo.record_event(req)
    events = await deps.proctoring_repo.list_events(req.session_id)
    score = compute_strike_score(events, now=datetime.now(UTC))
    ban_triggered = is_ban_threshold_exceeded(score, deps.settings.proctoring_strike_threshold)
    return ProctoringEventResponse(event=event, score=score, ban_triggered=ban_triggered)
