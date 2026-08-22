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

Version-aware (Phase 2 of the backend hardening plan): every function here
reads/updates ``ud.version`` (``live/state.py``'s ``InterviewUserdata``) so
the WS transport's repeated self-writes over one session's life keep
succeeding while a genuinely stale/concurrent writer gets rejected instead of
silently clobbering newer state. ``ud.version`` starts ``None`` for the
LiveKit worker path (it never sets it) — every call below treats ``None`` as
"skip the version check," reproducing that transport's old unconditional-
overwrite behavior exactly, unchanged.
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
        "expected_version": ud.version,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_api_base(settings)}/api/session/{session_id}/live-result",
                json=payload,
                headers=_internal_headers(settings),
            )
        if resp.status_code == 409:
            log.warning(
                "live: live-result POST for %s rejected as stale/concurrent "
                "(expected_version=%s): %s",
                session_id,
                ud.version,
                resp.text[:200],
            )
            return False
        if resp.status_code != 200:
            return False
        new_version = resp.json().get("version")
        if new_version is not None:
            ud.version = new_version
        return True
    except Exception:
        log.exception("live: live-result POST failed for %s", session_id)
        return False


async def flush_checkpoint(
    session_id: str,
    context: InterviewContext,
    transcript: list[dict],
    settings: Settings,
    *,
    expected_version: int | None = None,
) -> int | None:
    """Off-path partial persist for a periodic checkpointer (non-terminal,
    best-effort — network/other exceptions are still the caller's to swallow,
    unchanged). Signature still matches ``TranscriptFlusher``'s ``FlushFn``
    shape (context, transcript) once ``session_id``/``settings`` are bound via
    a closure at the call site — ``expected_version`` is a NEW keyword-only
    addition callers that don't know about it (``worker.py``'s closure) never
    pass, defaulting to ``None`` (skip the version check, the old
    unconditional-write behavior, exactly preserved for that transport).

    Returns the new version on success. Returns ``None`` — WITHOUT raising —
    on a 409 (stale version or already-terminal session): a checkpoint losing
    a race is informative, not exceptional, but it must not go on being
    silently swallowed the way it was before this changed (that's what
    originally hid the fact that a conflict is a real, meaningful signal).
    """
    payload = {
        "context": context.model_dump(),
        "transcript": transcript,
        "status": None,
        "expected_version": expected_version,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{_api_base(settings)}/api/session/{session_id}/live-result",
            json=payload,
            headers=_internal_headers(settings),
        )
    if resp.status_code == 409:
        log.warning(
            "live: checkpoint for %s rejected as stale/concurrent (expected_version=%s): %s",
            session_id,
            expected_version,
            resp.text[:200],
        )
        return None
    if resp.status_code != 200:
        return None
    return resp.json().get("version")


async def persist_via_repo(session_id: str, ud: InterviewUserdata, deps: Deps, *, has_answers: bool) -> bool:
    """Direct-store fallback (correct when both callers share Supabase).

    Uses the versioned, atomic ``save_live_result`` (Phase 2) instead of the
    old separate ``save_transcript``/``save_context``/``update_status`` calls
    — one atomic write instead of three, and the same terminal-status/
    version protection ``persist_via_api``'s HTTP route gets.
    """
    try:
        new_version = await deps.repo.save_live_result(
            session_id,
            context=ud.ctx,
            transcript=ud.transcript,
            status=None if has_answers else "no_answers",
            expected_version=ud.version,
        )
    except Exception:
        # A hard failure (not a version conflict — those return None cleanly
        # below), most likely an I/O error talking to the store itself.
        # Best-effort: mark the session errored so a later read shows an
        # honest failure state instead of silently looking untouched —
        # matches the original save_context-specific behavior this replaces.
        log.exception(
            "live: save_live_result FAILED for %s — answers may not be persisted; "
            "attempting to mark error",
            session_id,
        )
        try:
            await deps.repo.update_status(session_id, "error")
        except Exception:
            log.exception("live: update_status(error) failed for %s", session_id)
        return False
    if new_version is None:
        log.warning(
            "live: save_live_result for %s rejected — stale version (%s) or "
            "already terminal",
            session_id,
            ud.version,
        )
        return False
    ud.version = new_version
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
