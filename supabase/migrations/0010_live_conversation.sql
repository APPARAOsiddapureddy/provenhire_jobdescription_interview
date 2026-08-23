-- Durable storage for the live orchestrator's own runtime conversation state
-- (Phase 3 of the live-interview backend hardening plan: "reconnect persists
-- the LLM's own conversation history durably, not just cursor/answers").
--
-- Holds a JSON object shaped `{"persona": "...", "messages": [...]}` —
-- `LiveTurnSession.persona`/`.messages` (core/persistence/live_session_
-- repository.py's in-memory runtime state) mirrored to durable storage on
-- every per-turn checkpoint, so a reconnecting WS connection can restore the
-- model's actual conversation instead of losing it and re-greeting the
-- candidate. Separate from the existing `transcript` column (the flat,
-- question_id-tagged display/recovery log from Phase 1) and from `context`
-- (the durable business state / answers) — this column is orchestrator-
-- internal resume state only, never read by the web report.
--
-- Existing rows default to an empty object: `messages` absent there reads as
-- "no conversation to resume," which the WS handler treats as a signal to
-- start fresh rather than resume — exactly the state of every session that
-- predates this column.
alter table public.sessions
  add column if not exists live_conversation jsonb not null default '{}'::jsonb;
