-- Head-pose "looking away" analysis, added to the singleton
-- public.integrity_settings row (see 0007_integrity_settings.sql).
--
-- Defaults to 'off' so enabling it is an explicit choice: it is a
-- LOGGED-ONLY signal for human review, never a gate. Head pose at webcam
-- resolution cannot reliably distinguish "reading from a second screen"
-- from ordinary thinking-while-looking-away, an off-centre webcam, or a
-- candidate who avoids eye contact, so it deliberately carries no weight in
-- apps/agent/data/proctoring-weights.json and is excluded from
-- STRIKE_ELIGIBLE_RULES — it can never contribute to auto-ending an
-- interview.
alter table public.integrity_settings
  add column if not exists gaze_detection text not null default 'off'
    check (gaze_detection in ('off', 'monitor', 'strict'));
