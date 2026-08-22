"""API-only view models (not part of the wire contract in ``packages/shared``).

``SessionView`` is the read model behind ``GET /api/session/{id}``. It deliberately
lives here — NOT in ``shared_models`` — so the TS↔Pydantic parity check stays
untouched. It reports live prep progress (which agents have finished), any
input-quality warnings, and the assembled :class:`InterviewContext` once ready.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..core.session_status import SessionStatus
from ..shared_models import InterviewContext, ScoreCard

__all__ = ["PROGRESS_STEPS", "SessionStatus", "SessionView"]

# The canonical ordered set of prep steps a session progresses through. Reported
# back as completed-step keys (a subset/permutation of these) in SessionView.
PROGRESS_STEPS: tuple[str, ...] = (
    "cv_analysis",
    "jd_analysis",
    "company_research",
    "gap_matching",
    "general_round",
    "coding_round",
    "behavioral_round",
    "assemble_plan",
)


class SessionView(BaseModel):
    """Read model for a single session's status, progress and context."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: SessionStatus
    progress: list[str] = Field(default_factory=list)
    prep_warnings: list[str] = Field(default_factory=list)
    context: InterviewContext | None = None
    # The assembled scorecard once post-interview scoring has run (status
    # "complete"). Surfaced here so the web report can read it through the agent
    # API — no Supabase/RLS/auth on the read path (OSS runs without sign-in).
    scorecard: ScoreCard | None = None
    # Cumulative weighted-decay proctoring strike score (see core/proctoring/
    # scoring.py), composed in api/session.py's get_session handler — NOT
    # stored on the session row itself. None while the session has no
    # recorded proctoring events (nothing to compute) or has reached a
    # terminal status (no longer live, skip the lookup).
    proctoring_score: float | None = None
    # Optimistic-concurrency counter (core/persistence/repository.py's
    # save_live_result) — internal use by the live-interview transport, not
    # meaningful to the web report. Defaults to 1 so any caller constructing
    # a SessionView without it (existing code, existing tests) is unaffected;
    # the web app's Zod schema doesn't declare this field and silently
    # strips it (non-strict z.object), so this is a safe additive change.
    version: int = 1
