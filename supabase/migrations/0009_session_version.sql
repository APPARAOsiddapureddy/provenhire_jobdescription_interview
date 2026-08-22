-- Optimistic-concurrency version counter for public.sessions (Phase 2 of the
-- live-interview backend hardening plan). Every write from the versioned
-- repository methods (core/persistence/repository.py's save_live_result etc.)
-- goes through `UPDATE ... WHERE id = X AND version = expected` — a 0-row
-- response is the unambiguous "stale writer" signal, achievable with the
-- existing postgrest-py query builder alone (confirmed against the vendored
-- SDK source), no RPC/stored procedure needed.
--
-- Existing rows default to 1 so every already-persisted session has a valid
-- starting version the moment this migration runs. Unversioned callers
-- (the LiveKit worker path, not touched in this phase — see the plan's
-- backward-compatibility phase) are unaffected: they simply never pass an
-- expected_version, and the repository layer treats that as "skip the CAS
-- check" for backward compatibility, matching today's exact behavior.
alter table public.sessions
  add column if not exists version integer not null default 1;
