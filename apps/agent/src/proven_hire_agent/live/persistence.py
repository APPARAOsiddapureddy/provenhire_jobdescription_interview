"""Shared live-interview shutdown persistence + scoring trigger.

Extracted from ``worker.py``'s ``_persist_via_api``/``_persist_via_repo``/
score-trigger closures (Live Voice Pipeline Replacement, Phase 3) so both the
LiveKit worker (cross-process, scheduled for removal in Phase 8) and the new
WS-based orchestrator (``api/live.py``, in-process) share ONE implementation
during the transition instead of two copies drifting apart.

Kept as an HTTP POST to the agent-api's own ``/api/session/{id}/live-result``
route even for the new in-process caller (a loopback call) rather than
special-cased to call ``deps.repo`` directly — this preserves byte-identical
behavior between both callers while the worker still needs the cross-process
path; simplifying the in-process caller to skip the loopback is a reasonable
Phase 8 cleanup once the worker (and this dual-path need) is gone.
"""

from __future__ import annotations

import httpx

from ..core.config import Settings
from ..core.deps import Deps
from ..core.logging import get_logger
from ..shared_models import InterviewContext, ScoreRequest
from . import state
from .state import InterviewUserdata

log = get_logger(__name__)


def _api_base(settings: Settings) -> str:
    return (settings.agent_api_url or f"http://localhost:{settings.agent_api_port}").rstrip("/")


def _internal_headers(settings: Settings) -> dict[str, str]:
    secret = getattr(settings, "internal_api_secret", None)
    return {"X-Internal-Secret": secret} if secret else {}


async def persist_via_api(
    session_id: str, ud: InterviewUserdata, settings: Settings, *, has_answers: bool
) -> bool:
    """Persist through the agent-api's own write route. The worker runs in a
    SEPARATE process: with no Supabase configured its in-memory repo is not
    the one the API reads, so a direct repo write would land nowhere anyone
    looks — POST instead; ``persist_via_repo`` below is the fallback for
    shared-store (Supabase) deployments or if the POST itself fails.
    """
    payload = {
        "context": ud.ctx.model_dump(),
        "transcript": ud.transcript,
        "status": None if has_answers else "no_answers",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_api_base(settings)}/api/session/{session_id}/live-result",
                json=payload,
                headers=_internal_headers(settings),
            )
        return resp.status_code == 200
    except Exception:
        log.exception("live: live-result POST failed for %s", session_id)
        return False


async def flush_checkpoint(
    session_id: str, context: InterviewContext, transcript: list[dict], settings: Settings
) -> None:
    """Off-path partial persist for a periodic checkpointer (non-terminal,
    best-effort — any failure is the caller's to swallow). Signature matches
    ``TranscriptFlusher``'s ``FlushFn`` shape (context, transcript) once
    ``session_id``/``settings`` are bound via a closure/``functools.partial``
    at the call site — mirrors exactly how ``worker.py``'s own
    ``_flush_checkpoint`` closure captured them today.
    """
    payload = {"context": context.model_dump(), "transcript": transcript, "status": None}
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            f"{_api_base(settings)}/api/session/{session_id}/live-result",
            json=payload,
            headers=_internal_headers(settings),
        )


async def persist_via_repo(session_id: str, ud: InterviewUserdata, deps: Deps, *, has_answers: bool) -> bool:
    """Direct-store fallback (correct when both callers share Supabase)."""
    try:
        await deps.repo.save_transcript(session_id, ud.transcript)
    except Exception:
        log.exception("live: save_transcript failed for %s", session_id)
    try:
        await deps.repo.save_context(session_id, ud.ctx)
    except Exception:
        log.exception(
            "live: save_context FAILED for %s — answers not persisted; "
            "skipping scoring to avoid a blank scorecard",
            session_id,
        )
        try:
            await deps.repo.update_status(session_id, "error")
        except Exception:
            log.exception("live: update_status(error) failed for %s", session_id)
        return False
    if not has_answers:
        try:
            await deps.repo.update_status(session_id, "no_answers")
        except Exception:
            log.exception("live: update_status(no_answers) failed for %s", session_id)
    return True


async def trigger_scoring(session_id: str, settings: Settings) -> None:
    """Fire scoring best-effort; never raises. The score endpoint runs the
    full LLM pipeline inline, so this allows minutes, not seconds."""
    try:
        req = ScoreRequest(session_id=session_id)
        score_timeout = httpx.Timeout(10.0, read=600.0)
        async with httpx.AsyncClient(timeout=score_timeout) as client:
            await client.post(
                f"{_api_base(settings)}/api/score",
                json=req.model_dump(),
                headers=_internal_headers(settings),
            )
    except Exception:
        log.exception("live: scoring trigger failed for %s", session_id)


async def persist_and_score(session_id: str, ud: InterviewUserdata, deps: Deps) -> None:
    """The full end-of-session sequence: recover unsaved answers from the
    verbatim transcript, persist (API then repo fallback), and — only if
    something was actually persisted and there ARE real answers — trigger
    scoring. Shared by worker.py's ``_on_shutdown`` (until Phase 8) and the
    new WS handler's disconnect/end path.
    """
    recovered = state.reconstruct_answers(ud)
    if recovered:
        log.info("live: session %s recovered %d answer(s) from transcript", session_id, recovered)

    has_answers = any((a.transcript or "").strip() for a in ud.ctx.answers)

    persisted = await persist_via_api(session_id, ud, deps.settings, has_answers=has_answers)
    if not persisted:
        persisted = await persist_via_repo(session_id, ud, deps, has_answers=has_answers)
    if not persisted or not has_answers:
        if not has_answers:
            log.info("live: session %s has no answers; skipping scoring", session_id)
        return

    await trigger_scoring(session_id, deps.settings)
