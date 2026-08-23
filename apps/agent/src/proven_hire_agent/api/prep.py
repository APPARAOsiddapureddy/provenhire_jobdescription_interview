"""``POST /api/prep`` — kick off the prep pipeline for a CV + JD + company.

Returns immediately with a ``session_id`` (status ``prep``); the heavy pipeline
runs in a FastAPI ``BackgroundTask`` and the client polls ``GET /api/session/{id}``
for progress + the final context. (Under Starlette's ``TestClient`` the background
task runs to completion before the response is returned.)
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..core.deps import build_deps
from ..core.rate_limit import SlidingWindowRateLimiter, client_ip
from ..prep import run_prep_for_session
from ..shared_models import PrepRequest, PrepResponse

router = APIRouter()

# Route-level size ceilings (Starlette imposes NO default body limit). Enforced
# here rather than as max_length on the shared contract models so the generated
# JSON Schemas stay in TS<->Pydantic parity. cv_url is generous because the
# offline path sends the whole CV as pasted text / a data: URL in this field.
_MAX_CV_URL_LEN = 2_000_000  # ~1.5MB data-URL CV
_MAX_JD_LEN = 200_000
_MAX_COMPANY_LEN = 500

# Ingress rate limit (Phase 5, backend hardening plan): prep runs several
# real LLM calls per invocation (CV/JD/company analysis, question
# generation) — the most expensive single endpoint in the API. 5 per 5
# minutes is generous for legitimate re-tries (a bad CV URL, a validation
# rejection) while bounding a scripted-abuse loop.
_prep_limiter = SlidingWindowRateLimiter(max_events=5, window_sec=300.0)


@router.post("/api/prep", response_model=PrepResponse)
async def prep(req: PrepRequest, background_tasks: BackgroundTasks, request: Request) -> PrepResponse:
    if (
        len(req.cv_url) > _MAX_CV_URL_LEN
        or len(req.jd_text) > _MAX_JD_LEN
        or len(req.company) > _MAX_COMPANY_LEN
    ):
        raise HTTPException(status_code=413, detail="CV/JD/company input too large")
    ip = client_ip(request)
    if not _prep_limiter.allow(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many prep requests — please wait before starting another interview.",
            headers={"Retry-After": str(int(_prep_limiter.retry_after(ip)) + 1)},
        )
    deps = build_deps()
    session_id = await deps.repo.create_session(req)
    background_tasks.add_task(run_prep_for_session, session_id, req, deps)
    return PrepResponse(session_id=session_id)
