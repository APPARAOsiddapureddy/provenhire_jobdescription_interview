-- 0007_integrity_settings.sql — global proctoring/integrity-monitoring config.
-- Singleton row (id fixed to 1) edited from the open /settings/integrity page
-- and read by the live interview room at session start. Unlike `sessions`,
-- this table has no owning user — it's global, self-host-operator-level
-- config — so RLS is enabled with NO permissive policies at all: only the
-- service-role key (used exclusively by the agent's
-- IntegritySettingsRepository, mirroring how `sessions` is written) can read
-- or write it. The web app never queries this table directly; it proxies
-- through the agent's /api/integrity-settings, same as it does for sessions.

create table if not exists public.integrity_settings (
  id smallint primary key default 1 check (id = 1),
  ai_behavior_analysis text not null default 'off'
    check (ai_behavior_analysis in ('off', 'monitor', 'strict')),
  camera_required text not null default 'off'
    check (camera_required in ('off', 'monitor', 'strict')),
  copy_paste_detection text not null default 'off'
    check (copy_paste_detection in ('off', 'monitor', 'strict')),
  devtools_detection text not null default 'off'
    check (devtools_detection in ('off', 'monitor', 'strict')),
  fullscreen_required text not null default 'off'
    check (fullscreen_required in ('off', 'monitor', 'strict')),
  microphone_monitoring text not null default 'off'
    check (microphone_monitoring in ('off', 'monitor', 'strict')),
  camera_ai_detection text not null default 'off'
    check (camera_ai_detection in ('off', 'monitor', 'strict')),
  three_strike_auto_end text not null default 'off'
    check (three_strike_auto_end in ('off', 'monitor', 'strict')),
  screen_recording_enabled text not null default 'off'
    check (screen_recording_enabled in ('off', 'monitor', 'strict')),
  tab_switching_detection text not null default 'off'
    check (tab_switching_detection in ('off', 'monitor', 'strict')),
  updated_at timestamptz not null default now()
);

-- Seed the singleton row (idempotent — safe to re-run this migration).
insert into public.integrity_settings (id) values (1) on conflict (id) do nothing;

alter table public.integrity_settings enable row level security;
-- No policies created: RLS with zero permissive policies denies ALL access
-- to anon/authenticated roles by default. Only the service-role key (which
-- bypasses RLS entirely) can read or write — see the header comment.

drop trigger if exists integrity_settings_touch on public.integrity_settings;
create trigger integrity_settings_touch
  before update on public.integrity_settings
  for each row execute function public.touch_updated_at();
