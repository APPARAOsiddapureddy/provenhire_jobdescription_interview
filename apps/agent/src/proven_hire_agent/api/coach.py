"""``/api/coach`` — Study Coach plan + grounded chat (WP-4).

``POST /api/coach/plan`` takes a ``ScoreCard`` (the client already holds it from
the report) and returns a ``StudyPlan``. ``POST /api/coach/chat`` answers a
learner question, grounded in the knowledge base.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ..coach import run_coach_chat, run_coach_plan
from ..core.deps import build_deps
from ..core.rate_limit import SlidingWindowRateLimiter, client_ip
from ..shared_models import CoachChatRequest, CoachReply, ScoreCard, StudyPlan

router = APIRouter()

# Matches kb.py's _MAX_QUERY_LEN — a learner question has no legitimate need
# to be larger than a knowledge-base query, and Starlette has no default body
# size limit (see kb.py/prep.py), so this is the only thing bounding cost on
# an endpoint that spends LLM tokens per request.
_MAX_QUERY_LEN = 10_000

# Ingress rate limits (Phase 5, backend hardening plan) — each route spends
# real LLM tokens per call. Chat is naturally more frequent than plan
# generation (a back-and-forth conversation vs. a one-shot plan), so it
# gets a more generous budget.
_coach_plan_limiter = SlidingWindowRateLimiter(max_events=10, window_sec=300.0)
_coach_chat_limiter = SlidingWindowRateLimiter(max_events=30, window_sec=60.0)


class CoachPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scorecard: ScoreCard


@router.post("/api/coach/plan", response_model=StudyPlan)
async def coach_plan(req: CoachPlanRequest, request: Request) -> StudyPlan:
    ip = client_ip(request)
    if not _coach_plan_limiter.allow(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(int(_coach_plan_limiter.retry_after(ip)) + 1)},
        )
    return await run_coach_plan(req.scorecard, build_deps())


@router.post("/api/coach/chat", response_model=CoachReply)
async def coach_chat(req: CoachChatRequest, request: Request) -> CoachReply:
    if len(req.query) > _MAX_QUERY_LEN:
        raise HTTPException(status_code=413, detail="Query too large")
    ip = client_ip(request)
    if not _coach_chat_limiter.allow(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(int(_coach_chat_limiter.retry_after(ip)) + 1)},
        )
    return await run_coach_chat(req, build_deps())
