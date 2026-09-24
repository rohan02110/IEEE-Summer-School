-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run
--
-- Day numbering:
--   Day 0  = 2026-09-24  (Test Day)
--   Day 1  = 2026-09-26
--   Day 2  = 2026-09-27
--   Day 3  = 2026-09-28
--   Day 4  = 2026-09-29
--   Day 5  = 2026-09-30
--   Day 6  = 2026-10-01

create table if not exists participants (
  placeholder text primary key,          -- P001 .. P080
  roll_no     text,
  name        text
);

create table if not exists attendance (
  placeholder text    not null references participants(placeholder),
  day         integer not null check (day >= 0),   -- 0 = Test Day, 1-6 = event days
  lecture     integer not null default 1 check (lecture in (1, 2)),
  marked_at   timestamptz not null default now(),
  marked_by   text not null,
  primary key (placeholder, day, lecture)          -- one row per participant per day per lecture
);

create index if not exists attendance_day_lecture_idx on attendance (day, lecture);

create table if not exists scan_log (
  id        bigint generated always as identity primary key,
  ts        timestamptz not null default now(),
  code      text,
  result    text not null,
  volunteer text
);

-- Row Level Security: on, with no policies. The app connects with the
-- service_role key, which bypasses RLS entirely, so this just blocks any
-- other (anon/public) key from reading or writing these tables.
alter table participants enable row level security;
alter table attendance   enable row level security;
alter table scan_log     enable row level security;

