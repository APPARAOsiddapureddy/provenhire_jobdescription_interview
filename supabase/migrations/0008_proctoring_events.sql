-- Generic proctoring event log (Integrity Controls rework). `type` is
-- free-form text with NO check constraint / enum — adding a new detector
-- is just "pick a new string," no migration needed. Weight/decay/ban-
-- threshold math lives entirely in Python (core/proctoring/scoring.py),
-- never in SQL, so there is exactly one place that logic can drift.
--
-- Agent-written, not user-owned (like integrity_settings), so this uses
-- the same service-role-only RLS posture: enabled with zero policies. But
-- unlike integrity_settings (one singleton row), this is keyed per-session
-- — sessions.id is `text` (app-generated `sess_<hex>`, not a uuid), so the
-- foreign key here must be `text` too.
create table if not exists public.proctoring_events (
  id uuid primary key default gen_random_uuid(),
  session_id text not null references public.sessions(id) on delete cascade,
  type text not null,
  severity text not null default 'info'
    check (severity in ('info', 'warning', 'critical')),
  message text not null,
  -- A `data:` URL or an R2 https URL — never raw bytes (bytea). Only set
  -- for nonzero-weight events, not every diagnostic ping.
  photo text,
  created_at timestamptz not null default now()
);

create index if not exists proctoring_events_session_id_idx
  on public.proctoring_events (session_id, created_at);

-- Append-only log rows: no updated_at, no touch_updated_at() trigger.
alter table public.proctoring_events enable row level security;
-- Zero policies: service-role-only, same posture as integrity_settings.
-- Only the agent's service-role key can read or write this table.
