"""Session persistence.

The :class:`SessionRepository` protocol is the storage contract used by the prep
and post pipelines. :class:`MemoryRepository` is the default (a process-wide
singleton so a session written during ``POST /api/prep`` is visible to later
reads in the same process). :class:`SupabaseRepository` persists to the
``public.sessions`` table (see ``supabase/migrations/0001_init.sql``) and
lazy-imports the ``supabase`` SDK.

``version`` (``supabase/migrations/0009_session_version.sql``) is an
optimistic-concurrency counter, added for the live-interview backend hardening
plan. Only :meth:`SessionRepository.save_live_result` is version-aware — every
other write method here is UNCHANGED, unconditional-overwrite behavior,
preserved deliberately: retrofitting every write path (prep, scoring, the
LiveKit worker) with caller-tracked versions would be a sprawling, high-risk
change for problem that's specific to ONE path (two WS connections racing on
the same live-interview session). ``save_live_result`` covers exactly that
path — it's what both ``api/session.py``'s ``post_live_result`` route and
``live/persistence.py``'s in-process fallback call, replacing what used to be
2-3 separate unconditional writes plus a route-level, TOCTOU-racy terminal-
status check. Passing ``expected_version=None`` reproduces the OLD
unconditional-overwrite behavior exactly (still gated on the session not
already being terminal) — this is what the LiveKit worker path keeps doing,
unchanged; the new WS transport is the one that supplies a real version.

``live_conversation`` (``supabase/migrations/0010_live_conversation.sql``,
Phase 3) durably mirrors the live orchestrator's OWN runtime state — the raw
LLM message history + active persona (``LiveTurnSession.messages``/
``.persona`` in ``live/orchestrator.py``) — so a reconnecting WS connection
can resume the actual conversation instead of losing it and re-greeting the
candidate. Written via ``save_live_result``'s ``conversation`` parameter
(``None`` = don't touch, same convention as every other optional param here)
and read back via :meth:`SessionRepository.get_live_resume_state`, kept
deliberately separate from :class:`SessionView`/``get_session_view`` — this is
internal resume state for exactly one caller (``api/live.py``'s connect path),
never part of the general-purpose read model the web report loads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import uuid4

from ...api.views import SessionView
from ...shared_models import AnswerRecord, InterviewContext, PrepRequest, ScoreCard
from ..logging import get_logger
from ..session_status import ALLOWED_LIVE_STATUSES, TERMINAL_STATUSES

if TYPE_CHECKING:
    from ..config import Settings

log = get_logger(__name__)

# Bounded retry budget for the internal optimistic-retry read-modify-write
# methods (append_answer/mark_progress/add_warnings) — these race INSIDE one
# method call (read, mutate in Python, write back), independent of and in
# addition to save_live_result's caller-supplied version check. A handful of
# attempts is enough to ride out a genuine concurrent writer without risking
# a real infinite loop on a persistently-broken connection.
_INTERNAL_RETRY_ATTEMPTS = 5


def _new_session_id() -> str:
    return f"sess_{uuid4().hex}"


@dataclass(frozen=True)
class LiveResumeState:
    """Everything a reconnecting WS connection needs to resume a live
    session's in-memory runtime state (Phase 3): the durable flat transcript
    (Phase 1's question_id-tagged turns, needed so ``SessionGuard``'s turn-
    count ceiling and future ``add_turn`` calls see the FULL history, not
    just what happened after the reconnect) plus the raw LLM conversation +
    active persona (``live_conversation``).

    An empty ``messages`` list is the caller's own signal that this is a
    FRESH session — never opened, or a pre-Phase-3 row with nothing to
    resume — not a genuine reconnect; ``api/live.py`` branches on exactly
    that, not on this object being ``None`` (which only means "unknown
    session_id", already handled earlier by ``get_session_view``).
    """

    transcript: list[dict]
    persona: str
    messages: list[dict]


@runtime_checkable
class SessionRepository(Protocol):
    """Storage contract for interview sessions."""

    async def create_session(self, req: PrepRequest) -> str: ...

    async def save_context(self, session_id: str, ctx: InterviewContext) -> None: ...

    async def load_context(self, session_id: str) -> InterviewContext | None: ...

    async def update_status(self, session_id: str, status: str) -> None: ...

    async def append_answer(self, session_id: str, a: AnswerRecord) -> None: ...

    async def save_scorecard(self, session_id: str, sc: ScoreCard) -> None: ...

    async def save_transcript(self, session_id: str, turns: list[dict]) -> None: ...

    async def save_coach_transcript(self, session_id: str, turns: list[dict]) -> None: ...

    async def mark_progress(self, session_id: str, step: str) -> None: ...

    async def add_warnings(self, session_id: str, warnings: list[str]) -> None: ...

    async def get_session_view(self, session_id: str) -> SessionView | None: ...

    async def save_live_result(
        self,
        session_id: str,
        *,
        context: InterviewContext,
        transcript: list[dict] | None = None,
        status: str | None = None,
        expected_version: int | None = None,
        conversation: dict | None = None,
    ) -> int | None:
        """Atomically persist a live-interview write-back.

        Replaces the read-then-write-then-write-then-write sequence both
        ``api/session.py``'s route and ``live/persistence.py``'s in-process
        fallback used to do by hand. Returns the NEW version on success, or
        ``None`` if the write was rejected — either the session has already
        reached a terminal status (complete/no_answers/error/rejected), the
        supplied ``status`` isn't a legal live-result value, the session is
        unknown, or ``expected_version`` was supplied and didn't match the
        session's current version (a stale or second writer). ``None`` for
        ``expected_version`` skips that specific check (unconditional write,
        still gated on terminal-status) — the LiveKit worker path's exact
        existing behavior, preserved on purpose. ``conversation`` (Phase 3) is
        the same "``None`` = don't touch this column" convention — only the
        WS transport's per-turn checkpoint supplies it.
        """
        ...

    async def get_live_resume_state(self, session_id: str) -> LiveResumeState | None:
        """The durable state needed to resume a live WS session after a
        reconnect (Phase 3) — ``None`` only if ``session_id`` is unknown.
        See :class:`LiveResumeState` for how callers tell "fresh" from
        "resume."
        """
        ...


@dataclass
class _SessionRow:
    id: str
    status: str = "prep"
    # Owning user (Supabase auth uid); None for the offline/dev path. Stamped so
    # the web report's RLS read (auth.uid() = user_id) can see the row.
    user_id: str | None = None
    company: str | None = None
    cv_url: str | None = None
    jd_text: str | None = None
    language_mode: dict[str, Any] = field(default_factory=lambda: {"primary": "en", "mixed": False})
    context: dict[str, Any] | None = None
    scorecard: dict[str, Any] | None = None
    transcript: list[dict] | None = None
    # Spoken study-coach conversation — SEPARATE from the interview transcript
    # so a post-interview coach session can never overwrite the interview record.
    coach_transcript: list[dict] | None = None
    answers: list[dict] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Live orchestrator resume state (Phase 3) — see the module docstring.
    live_conversation: dict[str, Any] = field(default_factory=dict)
    # Optimistic-concurrency counter — see the module docstring. Bumped on
    # every mutating call, including the ones below that don't otherwise need
    # retry logic (no real race is possible without a yield point between a
    # read and a write, and nothing here awaits in between), so version
    # tracking stays consistent whichever repository a test/deployment uses.
    version: int = 1


class MemoryRepository:
    """In-memory repository. Status is tracked per row for test inspection."""

    def __init__(self) -> None:
        self._rows: dict[str, _SessionRow] = {}

    async def create_session(self, req: PrepRequest) -> str:
        session_id = _new_session_id()
        self._rows[session_id] = _SessionRow(
            id=session_id,
            status="prep",
            user_id=req.user_id,
            company=req.company,
            cv_url=req.cv_url,
            jd_text=req.jd_text,
            language_mode=req.language_mode.model_dump(),
        )
        return session_id

    async def save_context(self, session_id: str, ctx: InterviewContext) -> None:
        row = self._require(session_id)
        row.context = ctx.model_dump()
        row.version += 1

    async def load_context(self, session_id: str) -> InterviewContext | None:
        row = self._rows.get(session_id)
        if row is None or row.context is None:
            return None
        return InterviewContext.model_validate(row.context)

    async def update_status(self, session_id: str, status: str) -> None:
        row = self._require(session_id)
        row.status = status
        row.version += 1

    async def append_answer(self, session_id: str, a: AnswerRecord) -> None:
        row = self._require(session_id)
        row.answers.append(a.model_dump())
        # Persist into the canonical context too, so load_context() sees the
        # appended answer (mirrors SupabaseRepository).
        if row.context is not None:
            ctx = InterviewContext.model_validate(row.context)
            ctx.answers.append(a)
            row.context = ctx.model_dump()
        row.version += 1

    async def save_scorecard(self, session_id: str, sc: ScoreCard) -> None:
        row = self._require(session_id)
        row.scorecard = sc.model_dump()
        row.version += 1

    async def save_transcript(self, session_id: str, turns: list[dict]) -> None:
        row = self._require(session_id)
        row.transcript = list(turns)
        row.version += 1

    async def save_coach_transcript(self, session_id: str, turns: list[dict]) -> None:
        self._require(session_id).coach_transcript = list(turns)

    async def mark_progress(self, session_id: str, step: str) -> None:
        row = self._require(session_id)
        if step not in row.progress:
            row.progress.append(step)
            row.version += 1

    async def add_warnings(self, session_id: str, warnings: list[str]) -> None:
        row = self._require(session_id)
        changed = False
        for w in warnings:
            if w not in row.warnings:
                row.warnings.append(w)
                changed = True
        if changed:
            row.version += 1

    async def save_live_result(
        self,
        session_id: str,
        *,
        context: InterviewContext,
        transcript: list[dict] | None = None,
        status: str | None = None,
        expected_version: int | None = None,
        conversation: dict | None = None,
    ) -> int | None:
        row = self._rows.get(session_id)
        if row is None:
            return None
        if row.status in TERMINAL_STATUSES:
            return None
        if status is not None and status not in ALLOWED_LIVE_STATUSES:
            return None
        if expected_version is not None and row.version != expected_version:
            return None
        row.context = context.model_dump()
        if transcript is not None:
            row.transcript = list(transcript)
        if conversation is not None:
            row.live_conversation = dict(conversation)
        if status is not None:
            row.status = status
        row.version += 1
        return row.version

    async def get_live_resume_state(self, session_id: str) -> LiveResumeState | None:
        row = self._rows.get(session_id)
        if row is None:
            return None
        conv = row.live_conversation or {}
        return LiveResumeState(
            transcript=list(row.transcript or []),
            persona=conv.get("persona") or "interviewer",
            messages=list(conv.get("messages") or []),
        )

    async def get_session_view(self, session_id: str) -> SessionView | None:
        row = self._rows.get(session_id)
        if row is None:
            return None
        context = (
            InterviewContext.model_validate(row.context) if row.context else None
        )
        scorecard = (
            ScoreCard.model_validate(row.scorecard) if row.scorecard else None
        )
        return SessionView(
            session_id=row.id,
            status=row.status,
            progress=list(row.progress),
            prep_warnings=list(row.warnings),
            context=context,
            scorecard=scorecard,
            version=row.version,
        )

    # --- test / inspection helpers (not part of the protocol) ----------------
    def get_status(self, session_id: str) -> str | None:
        row = self._rows.get(session_id)
        return row.status if row else None

    def _require(self, session_id: str) -> _SessionRow:
        row = self._rows.get(session_id)
        if row is None:
            raise KeyError(f"Unknown session_id: {session_id}")
        return row


class SupabaseRepository:
    """Persist sessions to Supabase ``public.sessions`` (lazy ``supabase`` SDK)."""

    def __init__(self, url: str, service_role_key: str) -> None:
        self._url = url
        self._key = service_role_key
        self._client: Any | None = None

    def _table(self) -> Any:
        if self._client is None:
            try:
                from supabase import create_client
            except ImportError as exc:  # pragma: no cover - depends on optional SDK
                raise RuntimeError(
                    "supabase is not installed; install the 'supabase' extra."
                ) from exc
            self._client = create_client(self._url, self._key)
        return self._client.table("sessions")

    async def _exec(self, build: Any) -> Any:
        import asyncio

        return await asyncio.to_thread(build)

    async def create_session(self, req: PrepRequest) -> str:
        session_id = _new_session_id()
        payload = {
            "id": session_id,
            "status": "prep",
            # Stamp the owner so the web report's RLS read (auth.uid() = user_id)
            # can see this row. None on the offline/dev path (column is nullable).
            "user_id": req.user_id,
            "company": req.company,
            "cv_url": req.cv_url,
            "jd_text": req.jd_text,
            "language_mode": req.language_mode.model_dump(),
            "progress": [],
            "prep_warnings": [],
        }
        await self._exec(lambda: self._table().insert(payload).execute())
        return session_id

    async def save_context(self, session_id: str, ctx: InterviewContext) -> None:
        await self._update(session_id, {"context": ctx.model_dump()})

    async def load_context(self, session_id: str) -> InterviewContext | None:
        def _build() -> Any:
            return self._table().select("context").eq("id", session_id).limit(1).execute()

        resp = await self._exec(_build)
        rows = getattr(resp, "data", None) or []
        if not rows or not rows[0].get("context"):
            return None
        return InterviewContext.model_validate(rows[0]["context"])

    async def update_status(self, session_id: str, status: str) -> None:
        await self._update(session_id, {"status": status})

    async def _read_modify_write_retry(
        self,
        session_id: str,
        select_cols: str,
        compute_update: Any,  # Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> bool:
        """Read ``select_cols`` + ``version``, call ``compute_update(row)`` to
        get the new column values to write (or ``None`` for "no change
        needed"), and write conditionally on the version just read — retrying
        up to ``_INTERNAL_RETRY_ATTEMPTS`` times if a concurrent writer wins
        the race in between. Closes a real, independent read-modify-write
        race in append_answer/mark_progress/add_warnings below: each already
        did its own select-then-mutate-then-update with no version check at
        all, so two concurrent calls could each read the same array and each
        overwrite the other's append.
        """
        for _attempt in range(_INTERNAL_RETRY_ATTEMPTS):

            def _build_select() -> Any:
                return (
                    self._table()
                    .select(f"{select_cols},version")
                    .eq("id", session_id)
                    .limit(1)
                    .execute()
                )

            resp = await self._exec(_build_select)
            rows = getattr(resp, "data", None) or []
            if not rows:
                return False
            row = rows[0]
            # .get(key, default) only falls back when the KEY is absent, not
            # when its value is explicitly None (e.g. an older row / a test
            # fixture predating this column) — the real Postgres column is
            # NOT NULL DEFAULT 1, so None here only ever means "unknown,
            # treat as the default."
            version = row.get("version") or 1
            updates = compute_update(row)
            if updates is None:
                return True  # nothing to change — not a conflict, just a no-op
            updates["version"] = version + 1

            def _build_update(updates: dict[str, Any] = updates, version: int = version) -> Any:
                # Default-arg capture, not closure-over-loop-var: each retry
                # iteration reassigns updates/version before this is (re)defined,
                # and this callable is always invoked+awaited before the loop
                # moves on — but binding explicitly avoids any ambiguity.
                return (
                    self._table()
                    .update(updates)
                    .eq("id", session_id)
                    .eq("version", version)
                    .execute()
                )

            resp2 = await self._exec(_build_update)
            if getattr(resp2, "data", None):
                return True
            # 0 rows updated: a concurrent writer changed the version between
            # our read and write. Re-read and retry rather than lose this
            # write silently.
        log.warning(
            "repository: read-modify-write retry budget (%d) exhausted for %s",
            _INTERNAL_RETRY_ATTEMPTS,
            session_id,
        )
        return False

    async def append_answer(self, session_id: str, a: AnswerRecord) -> None:
        def _compute(row: dict[str, Any]) -> dict[str, Any] | None:
            ctx_data = row.get("context")
            if not ctx_data:
                return None
            ctx = InterviewContext.model_validate(ctx_data)
            ctx.answers.append(a)
            return {"context": ctx.model_dump()}

        await self._read_modify_write_retry(session_id, "context", _compute)

    async def save_scorecard(self, session_id: str, sc: ScoreCard) -> None:
        await self._update(session_id, {"scorecard": sc.model_dump()})

    async def save_transcript(self, session_id: str, turns: list[dict]) -> None:
        await self._update(session_id, {"transcript": list(turns)})

    async def save_coach_transcript(self, session_id: str, turns: list[dict]) -> None:
        # Requires the coach_transcript column (migration 0004); callers treat
        # this as best-effort, so a missing column logs rather than crashes.
        await self._update(session_id, {"coach_transcript": list(turns)})

    async def mark_progress(self, session_id: str, step: str) -> None:
        def _compute(row: dict[str, Any]) -> dict[str, Any] | None:
            progress = list(row.get("progress") or [])
            if step in progress:
                return None
            progress.append(step)
            return {"progress": progress}

        await self._read_modify_write_retry(session_id, "progress", _compute)

    async def add_warnings(self, session_id: str, warnings: list[str]) -> None:
        def _compute(row: dict[str, Any]) -> dict[str, Any] | None:
            existing = list(row.get("prep_warnings") or [])
            changed = False
            for w in warnings:
                if w not in existing:
                    existing.append(w)
                    changed = True
            return {"prep_warnings": existing} if changed else None

        await self._read_modify_write_retry(session_id, "prep_warnings", _compute)

    async def save_live_result(
        self,
        session_id: str,
        *,
        context: InterviewContext,
        transcript: list[dict] | None = None,
        status: str | None = None,
        expected_version: int | None = None,
        conversation: dict | None = None,
    ) -> int | None:
        if status is not None and status not in ALLOWED_LIVE_STATUSES:
            return None

        def _build_select() -> Any:
            return self._table().select("status,version").eq("id", session_id).limit(1).execute()

        resp = await self._exec(_build_select)
        rows = getattr(resp, "data", None) or []
        if not rows:
            return None
        current_status = rows[0].get("status", "prep")
        current_version = rows[0].get("version") or 1
        if current_status in TERMINAL_STATUSES:
            return None
        if expected_version is not None and current_version != expected_version:
            return None

        values: dict[str, Any] = {"context": context.model_dump(), "version": current_version + 1}
        if transcript is not None:
            values["transcript"] = list(transcript)
        if conversation is not None:
            values["live_conversation"] = dict(conversation)
        if status is not None:
            values["status"] = status

        def _build_update() -> Any:
            return (
                self._table()
                .update(values)
                .eq("id", session_id)
                .eq("version", current_version)
                .execute()
            )

        resp2 = await self._exec(_build_update)
        if not getattr(resp2, "data", None):
            # Lost the race between our read and write — a genuinely stale
            # writer (or a second live connection). Never retry-and-clobber
            # here: this conflict is a meaningful signal the caller needs,
            # not something to silently paper over.
            return None
        return current_version + 1

    async def get_live_resume_state(self, session_id: str) -> LiveResumeState | None:
        def _build() -> Any:
            return (
                self._table()
                .select("transcript,live_conversation")
                .eq("id", session_id)
                .limit(1)
                .execute()
            )

        resp = await self._exec(_build)
        rows = getattr(resp, "data", None) or []
        if not rows:
            return None
        row = rows[0]
        conv = row.get("live_conversation") or {}
        return LiveResumeState(
            transcript=list(row.get("transcript") or []),
            persona=conv.get("persona") or "interviewer",
            messages=list(conv.get("messages") or []),
        )

    async def get_session_view(self, session_id: str) -> SessionView | None:
        def _build() -> Any:
            return (
                self._table()
                .select("id,status,progress,prep_warnings,context,scorecard,version")
                .eq("id", session_id)
                .limit(1)
                .execute()
            )

        resp = await self._exec(_build)
        rows = getattr(resp, "data", None) or []
        if not rows:
            return None
        row = rows[0]
        ctx_data = row.get("context")
        context = InterviewContext.model_validate(ctx_data) if ctx_data else None
        sc_data = row.get("scorecard")
        scorecard = ScoreCard.model_validate(sc_data) if sc_data else None
        return SessionView(
            session_id=row["id"],
            status=row.get("status", "prep"),
            progress=list(row.get("progress") or []),
            prep_warnings=list(row.get("prep_warnings") or []),
            context=context,
            scorecard=scorecard,
            version=row.get("version") or 1,
        )

    async def _update(self, session_id: str, values: dict[str, Any]) -> None:
        await self._exec(lambda: self._table().update(values).eq("id", session_id).execute())


# Module-wide singleton so MemoryRepository state survives across build_deps() calls.
_MEMORY_REPO = MemoryRepository()


def get_repository(settings: Settings) -> SessionRepository:
    """Return a repository: Supabase if fully configured, else the memory singleton."""
    if settings.supabase_url and settings.supabase_service_role_key:
        return SupabaseRepository(settings.supabase_url, settings.supabase_service_role_key)
    if settings.supabase_url or settings.supabase_service_role_key:
        # Half-configured Supabase is almost always a deployment mistake; say so
        # loudly instead of silently dropping every session into process memory.
        log.error(
            "Supabase is PARTIALLY configured (need BOTH SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY); falling back to the in-memory store — "
            "sessions will NOT survive a restart."
        )
    return _MEMORY_REPO
