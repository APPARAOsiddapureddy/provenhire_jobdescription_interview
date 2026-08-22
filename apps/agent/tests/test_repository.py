"""Offline tests for the session repositories.

``MemoryRepository`` is exercised directly. ``SupabaseRepository`` — the
PRODUCTION persistence of the live-result write-back, scorecard save, status
transitions, and the session-view read — is exercised through an injected fake
recording client: ``_table()`` only imports the optional ``supabase`` SDK when
``self._client is None``, so setting ``repo._client`` to a postgrest-shaped
fake runs every real repository method offline (conftest blanks the creds, so
nothing else in the suite ever constructs this class). The fake JSON-encodes
every write payload exactly where the real SDK would, pinning that python-mode
``model_dump()`` payloads stay JSON-safe on the hosted path too.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from proven_hire_agent.core.adapters.mock import build_mock
from proven_hire_agent.core.persistence.repository import (
    MemoryRepository,
    SupabaseRepository,
)
from proven_hire_agent.shared_models import (
    AnswerRecord,
    InterviewContext,
    LanguageMode,
    PrepRequest,
    ScoreCard,
)

# apps/agent/tests/ -> repo root -> the migrations that define public.sessions.
_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "supabase" / "migrations"


def _run(coro):
    return asyncio.run(coro)


def _prep_request() -> PrepRequest:
    return PrepRequest(
        cv_url="https://example.com/cv.pdf",
        jd_text="We are hiring a backend engineer.",
        company="Acme Payments",
        language_mode=LanguageMode(primary="en", mixed=False),
    )


def test_create_save_load_round_trip() -> None:
    repo = MemoryRepository()
    session_id = _run(repo.create_session(_prep_request()))
    assert session_id.startswith("sess_")
    assert repo.get_status(session_id) == "prep"

    ctx = build_mock(InterviewContext)
    assert isinstance(ctx, InterviewContext)
    _run(repo.save_context(session_id, ctx))

    loaded = _run(repo.load_context(session_id))
    assert loaded is not None
    assert loaded.model_dump() == ctx.model_dump()


def test_create_session_stamps_user_id() -> None:
    """Regression (report RLS bug, PR #5): the owning user must land on the row.

    Dropping the ``user_id=req.user_id`` stamp would silently pass the rest of
    the suite while breaking the hosted layer's RLS ownership read
    (``auth.uid() = user_id`` in supabase/migrations/0001_init.sql).
    """
    repo = MemoryRepository()
    owner = "11111111-2222-3333-4444-555555555555"
    req = _prep_request().model_copy(update={"user_id": owner})
    session_id = _run(repo.create_session(req))
    assert repo._rows[session_id].user_id == owner

    # The offline/no-auth path stays ownerless (None), never an empty string.
    anon_id = _run(repo.create_session(_prep_request()))
    assert repo._rows[anon_id].user_id is None


def test_save_coach_transcript_does_not_touch_interview_transcript() -> None:
    """The spoken coach's log persists separately from the interview record."""
    repo = MemoryRepository()
    session_id = _run(repo.create_session(_prep_request()))
    interview = [{"role": "user", "text": "my interview answer"}]
    coach = [{"role": "assistant", "text": "let's drill system design"}]
    _run(repo.save_transcript(session_id, interview))
    _run(repo.save_coach_transcript(session_id, coach))
    row = repo._rows[session_id]
    assert row.transcript == interview
    assert row.coach_transcript == coach


def test_update_status_and_missing_load() -> None:
    repo = MemoryRepository()
    session_id = _run(repo.create_session(_prep_request()))
    _run(repo.update_status(session_id, "ready"))
    assert repo.get_status(session_id) == "ready"
    # A session with no saved context returns None.
    assert _run(repo.load_context("sess_does_not_exist")) is None


def test_append_answer_and_save_scorecard() -> None:
    repo = MemoryRepository()
    session_id = _run(repo.create_session(_prep_request()))

    answer = AnswerRecord(
        question_id="q1",
        transcript="A mock answer.",
        started_at="2026-06-08T09:00:00Z",
        ended_at="2026-06-08T09:01:00Z",
    )
    _run(repo.append_answer(session_id, answer))

    scorecard = build_mock(ScoreCard)
    assert isinstance(scorecard, ScoreCard)
    _run(repo.save_scorecard(session_id, scorecard))

    _run(repo.save_transcript(session_id, [{"role": "agent", "text": "hi"}]))


# --- SupabaseRepository via an injected fake recording client -----------------


class _FakeSupabaseResponse:
    def __init__(self, data: list[dict]) -> None:
        self.data = data


class _FakeSessionsTable:
    """One postgrest-style chained call (insert/update/select … execute).

    Executes against a shared in-memory row store and appends
    ``(op, payload_or_columns, session_id)`` to the shared log. Every WRITE
    payload is ``json.dumps``-encoded first — the boundary where the real SDK
    serializes — so a non-JSON type (datetime/enum) added to a model breaks
    these tests instead of only the hosted deployment.
    """

    def __init__(self, store: dict[str, dict], log: list[tuple]) -> None:
        self._store = store
        self._log = log
        self._op: str | None = None
        self._payload: Any = None
        self._cols: str | None = None
        self._id: str | None = None
        # Optimistic-concurrency filter (Phase 2): set only when the caller
        # chains ``.eq("version", ...)`` onto an update, i.e. every CAS write
        # save_live_result/the read-modify-write retry helper issue. ``None``
        # means no version filter was applied — every write this fixture saw
        # before Phase 2, still exercised by the plain ``_update`` callers.
        self._version: int | None = None

    def insert(self, payload: dict) -> _FakeSessionsTable:
        self._op, self._payload = "insert", payload
        return self

    def update(self, values: dict) -> _FakeSessionsTable:
        self._op, self._payload = "update", values
        return self

    def select(self, cols: str) -> _FakeSessionsTable:
        self._op, self._cols = "select", cols
        return self

    def eq(self, col: str, value: Any) -> _FakeSessionsTable:
        assert col in ("id", "version"), (
            "the repository only ever filters by primary key or, for CAS "
            "writes (Phase 2), the optimistic-concurrency version"
        )
        if col == "id":
            self._id = value
        else:
            self._version = value
        return self

    def limit(self, n: int) -> _FakeSessionsTable:
        return self

    def execute(self) -> _FakeSupabaseResponse:
        if self._op == "insert":
            json.dumps(self._payload)  # the SDK JSON-encodes; non-JSON fails HERE
            self._log.append(("insert", self._payload, self._payload["id"]))
            self._store[self._payload["id"]] = dict(self._payload)
            return _FakeSupabaseResponse([self._payload])
        if self._op == "update":
            json.dumps(self._payload)
            self._log.append(("update", self._payload, self._id))
            row = self._store.get(self._id or "")
            # Simulate Postgres's WHERE version = <n>: a mismatch (including
            # the fixture's "column predates this row" None-as-1 default)
            # matches zero rows — the real row is left untouched and the
            # caller sees an empty response, exactly like a real CAS conflict.
            if row is not None and self._version is not None:
                current_version = row.get("version") or 1
                if current_version != self._version:
                    row = None
            if row is not None:
                row.update(self._payload)
            return _FakeSupabaseResponse([row] if row is not None else [])
        assert self._op == "select"
        self._log.append(("select", self._cols, self._id))
        row = self._store.get(self._id or "")
        if row is None:
            return _FakeSupabaseResponse([])
        cols = [c.strip() for c in (self._cols or "").split(",")]
        return _FakeSupabaseResponse([{c: row.get(c) for c in cols}])


class _FakeSupabaseClient:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.log: list[tuple] = []

    def table(self, name: str) -> _FakeSessionsTable:
        assert name == "sessions", "all session persistence lives in public.sessions"
        return _FakeSessionsTable(self.rows, self.log)


def _supabase_repo() -> tuple[SupabaseRepository, _FakeSupabaseClient]:
    repo = SupabaseRepository("https://example.supabase.co", "service-role-key")
    fake = _FakeSupabaseClient()
    repo._client = fake  # _table() only imports the SDK when _client is None
    return repo, fake


def test_supabase_create_and_context_round_trip_payloads_are_json_safe() -> None:
    """create_session/save_context write python-mode ``model_dump()`` payloads:
    they must stay JSON-encodable AND round-trip through ``load_context`` —
    the exact read the scoring pipeline performs on the hosted path."""
    repo, fake = _supabase_repo()
    session_id = _run(repo.create_session(_prep_request()))
    assert session_id.startswith("sess_")
    assert fake.rows[session_id]["status"] == "prep"
    assert fake.rows[session_id]["jd_text"] == "We are hiring a backend engineer."

    ctx = build_mock(InterviewContext)
    assert isinstance(ctx, InterviewContext)
    _run(repo.save_context(session_id, ctx))  # raises in execute() if non-JSON

    loaded = _run(repo.load_context(session_id))
    assert loaded is not None
    assert loaded.model_dump() == ctx.model_dump()
    # Unknown ids read as None, never raise (the worker treats this as "not ready").
    assert _run(repo.load_context("sess_missing")) is None


def test_supabase_create_session_stamps_user_id_column() -> None:
    """The RLS ownership column (report bug, PR #5) must land on the INSERT
    payload on the Supabase path too — auth.uid() = user_id reads depend on it."""
    repo, fake = _supabase_repo()
    owner = "11111111-2222-3333-4444-555555555555"
    sid = _run(repo.create_session(_prep_request().model_copy(update={"user_id": owner})))
    assert fake.rows[sid]["user_id"] == owner
    anon = _run(repo.create_session(_prep_request()))
    assert fake.rows[anon]["user_id"] is None


def test_supabase_update_status_writes_each_live_terminal_status() -> None:
    """The live path's status transitions (no_answers / error / complete) must
    each become a ``{"status": ...}`` update against the row."""
    repo, fake = _supabase_repo()
    sid = _run(repo.create_session(_prep_request()))
    for status in ("no_answers", "error", "complete"):
        _run(repo.update_status(sid, status))
        assert fake.rows[sid]["status"] == status
        assert ("update", {"status": status}, sid) in fake.log


def test_supabase_save_scorecard_payload_is_json_encodable() -> None:
    """save_scorecard ships ``sc.model_dump()`` (python mode) to the SDK: it
    must JSON-encode and land in the ``scorecard`` column unchanged."""
    repo, fake = _supabase_repo()
    sid = _run(repo.create_session(_prep_request()))
    sc = build_mock(ScoreCard)
    assert isinstance(sc, ScoreCard)
    _run(repo.save_scorecard(sid, sc))  # raises in execute() if non-JSON
    assert fake.rows[sid]["scorecard"] == sc.model_dump()


def test_supabase_append_answer_read_modify_writes_the_context_blob() -> None:
    """Supabase ``append_answer`` mutates the canonical context blob (the
    Memory/Supabase asymmetry test_score.py's docstring warns about): the
    appended answer must be visible to a later ``load_context``. With no
    context saved yet it is a silent no-op (no update issued)."""
    repo, fake = _supabase_repo()
    sid = _run(repo.create_session(_prep_request()))
    ctx = build_mock(InterviewContext)
    assert isinstance(ctx, InterviewContext)
    base_answers = len(ctx.answers)
    _run(repo.save_context(sid, ctx))

    answer = AnswerRecord(
        question_id="q1",
        transcript="A real spoken answer.",
        started_at="2026-06-11T09:00:00Z",
        ended_at="2026-06-11T09:01:00Z",
    )
    _run(repo.append_answer(sid, answer))

    loaded = _run(repo.load_context(sid))
    assert loaded is not None
    assert len(loaded.answers) == base_answers + 1
    assert loaded.answers[-1].model_dump() == answer.model_dump()

    # No context yet -> append must not write anything.
    sid2 = _run(repo.create_session(_prep_request()))
    updates_before = len([op for op, *_ in fake.log if op == "update"])
    _run(repo.append_answer(sid2, answer))
    assert len([op for op, *_ in fake.log if op == "update"]) == updates_before


def test_supabase_get_session_view_selects_migration_columns_and_maps_row() -> None:
    """``get_session_view`` must select exactly the columns the migrations
    create and map them onto SessionView (status/progress/prep_warnings/
    context/scorecard) — a renamed or dropped column fails here, not only in
    the hosted deployment."""
    repo, fake = _supabase_repo()
    sid = _run(repo.create_session(_prep_request()))
    ctx = build_mock(InterviewContext)
    sc = build_mock(ScoreCard)
    _run(repo.save_context(sid, ctx))
    _run(repo.save_scorecard(sid, sc))
    _run(repo.mark_progress(sid, "cv_analysis"))
    _run(repo.mark_progress(sid, "cv_analysis"))  # idempotent: no duplicate
    _run(repo.add_warnings(sid, ["JD text is very short."]))
    _run(repo.update_status(sid, "complete"))

    view = _run(repo.get_session_view(sid))
    assert view is not None
    assert (view.session_id, view.status) == (sid, "complete")
    assert view.progress == ["cv_analysis"]
    assert view.prep_warnings == ["JD text is very short."]
    assert view.context is not None
    assert view.context.model_dump() == ctx.model_dump()
    assert view.scorecard is not None
    assert view.scorecard.model_dump() == sc.model_dump()

    # Pin the select column list against what the migrations actually create.
    select_cols = [cols for op, cols, row_id in fake.log if op == "select" and row_id == sid][-1]
    assert select_cols == "id,status,progress,prep_warnings,context,scorecard,version"
    migration_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    assert migration_files, f"no migrations found under {_MIGRATIONS_DIR}"
    migrations_sql = "".join(p.read_text() for p in migration_files)
    for col in select_cols.split(","):
        assert col in migrations_sql, f"selected column {col!r} not defined by any migration"

    # Unknown ids map to None (the API turns this into a 404, not a 500).
    assert _run(repo.get_session_view("sess_missing")) is None


# --- save_live_result: versioned, atomic, transition-protected (Phase 2) ------


def _ctx() -> InterviewContext:
    ctx = build_mock(InterviewContext)
    assert isinstance(ctx, InterviewContext)
    return ctx


def test_memory_save_live_result_two_sequential_writes_from_the_same_session_succeed() -> None:
    """A session's own repeated writes (the per-turn checkpoint pattern) keep
    succeeding as long as each supplies the version it just got back."""
    repo = MemoryRepository()
    sid = _run(repo.create_session(_prep_request()))
    v0 = repo._rows[sid].version

    v1 = _run(repo.save_live_result(sid, context=_ctx(), expected_version=v0))
    assert v1 == v0 + 1

    v2 = _run(repo.save_live_result(sid, context=_ctx(), expected_version=v1))
    assert v2 == v1 + 1
    assert repo._rows[sid].version == v2


def test_memory_save_live_result_stale_version_is_rejected_without_corrupting_state() -> None:
    """A second writer using the version it read BEFORE another write landed
    must be rejected — and the winning write's state must survive untouched."""
    repo = MemoryRepository()
    sid = _run(repo.create_session(_prep_request()))
    v0 = repo._rows[sid].version

    winning_ctx = _ctx()
    v1 = _run(repo.save_live_result(sid, context=winning_ctx, expected_version=v0))
    assert v1 == v0 + 1

    # A stale writer still holding v0 (e.g. a reconnecting duplicate tab).
    stale_ctx = _ctx()
    result = _run(repo.save_live_result(sid, context=stale_ctx, expected_version=v0))
    assert result is None

    # The winning write's context and version are untouched by the rejected one.
    assert repo._rows[sid].version == v1
    assert repo._rows[sid].context == winning_ctx.model_dump()


def test_memory_save_live_result_terminal_session_rejects_write_regardless_of_version() -> None:
    """Once a session reaches a terminal status, no write lands — even one
    carrying the exact current version (the DB-write equivalent of a stale
    reconnect racing the interview's own completion)."""
    repo = MemoryRepository()
    sid = _run(repo.create_session(_prep_request()))
    v1 = _run(repo.save_live_result(sid, context=_ctx(), status="no_answers", expected_version=1))
    assert v1 is not None
    assert repo._rows[sid].status == "no_answers"

    result = _run(repo.save_live_result(sid, context=_ctx(), expected_version=v1))
    assert result is None
    assert repo._rows[sid].version == v1  # untouched


def test_supabase_save_live_result_two_sequential_writes_from_the_same_session_succeed() -> None:
    repo, fake = _supabase_repo()
    sid = _run(repo.create_session(_prep_request()))

    v1 = _run(repo.save_live_result(sid, context=_ctx(), expected_version=1))
    assert v1 == 2
    assert fake.rows[sid]["version"] == 2

    v2 = _run(repo.save_live_result(sid, context=_ctx(), expected_version=v1))
    assert v2 == 3
    assert fake.rows[sid]["version"] == 3


def test_supabase_save_live_result_stale_version_is_rejected_without_corrupting_state() -> None:
    repo, fake = _supabase_repo()
    sid = _run(repo.create_session(_prep_request()))

    winning_ctx = _ctx()
    v1 = _run(repo.save_live_result(sid, context=winning_ctx, expected_version=1))
    assert v1 == 2

    stale_result = _run(repo.save_live_result(sid, context=_ctx(), expected_version=1))
    assert stale_result is None
    assert fake.rows[sid]["version"] == 2
    assert fake.rows[sid]["context"] == winning_ctx.model_dump()


def test_supabase_save_live_result_terminal_session_rejects_write_regardless_of_version() -> None:
    repo, fake = _supabase_repo()
    sid = _run(repo.create_session(_prep_request()))
    v1 = _run(repo.save_live_result(sid, context=_ctx(), status="error", expected_version=1))
    assert v1 == 2
    assert fake.rows[sid]["status"] == "error"

    result = _run(repo.save_live_result(sid, context=_ctx(), expected_version=v1))
    assert result is None
    assert fake.rows[sid]["version"] == 2


def test_supabase_save_live_result_disallowed_status_is_rejected_before_any_read() -> None:
    """A status outside {no_answers, error} must never even reach the store —
    checked up front so it can't race a legitimate concurrent write."""
    repo, fake = _supabase_repo()
    sid = _run(repo.create_session(_prep_request()))
    result = _run(repo.save_live_result(sid, context=_ctx(), status="complete", expected_version=1))
    assert result is None
    assert fake.rows[sid]["status"] == "prep"
    assert not [op for op in fake.log if op[0] in ("select", "update")]


def test_supabase_append_answer_retries_past_a_racing_writer() -> None:
    """append_answer's internal read-modify-write race (distinct from
    save_live_result's caller-supplied CAS): a second writer bumps the row's
    version in the gap between this call's own read and write. The retry loop
    must re-read and still land the answer, never silently lose it."""
    repo, fake = _supabase_repo()
    sid = _run(repo.create_session(_prep_request()))
    ctx = _ctx()
    base_answers = len(ctx.answers)
    _run(repo.save_context(sid, ctx))
    log_before_append = len(fake.log)

    original_exec = repo._exec
    call_count = {"n": 0}

    async def racing_exec(build: Any) -> Any:
        resp = await original_exec(build)
        call_count["n"] += 1
        # Race exactly once, right after append_answer's own first read —
        # simulates a second writer landing its write before ours does.
        if call_count["n"] == 1:
            fake.rows[sid]["version"] = (fake.rows[sid].get("version") or 1) + 1
        return resp

    repo._exec = racing_exec  # type: ignore[method-assign]

    answer = AnswerRecord(
        question_id="q1",
        transcript="Recovered despite a racing writer.",
        started_at="2026-06-11T09:00:00Z",
        ended_at="2026-06-11T09:01:00Z",
    )
    _run(repo.append_answer(sid, answer))

    loaded = _run(repo.load_context(sid))
    assert loaded is not None
    assert len(loaded.answers) == base_answers + 1
    assert loaded.answers[-1].model_dump() == answer.model_dump()
    # One lost attempt (0-row conflict) plus the winning retry.
    updates = [op for op in fake.log[log_before_append:] if op[0] == "update" and op[2] == sid]
    assert len(updates) == 2
