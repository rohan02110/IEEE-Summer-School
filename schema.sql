-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run

create table if not exists participants (
  placeholder text primary key,          -- P001 .. P080
  roll_no     text,
  name        text
);

create table if not exists attendance (
  placeholder text not null references participants(placeholder),
  day         integer not null,
  session     integer not null default 1 check (session in (1, 2)),
  marked_at   timestamptz not null default now(),
  marked_by   text not null,
  primary key (placeholder, day, session) -- one row per participant per day per lecture session
);

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

-- -----------------------------------------------------------------------------
-- MIGRATION SCRIPT (if attendance table already exists with (placeholder, day) PK):
-- -----------------------------------------------------------------------------
-- alter table attendance add column if not exists session integer not null default 1 check (session in (1, 2));
-- alter table attendance drop constraint if exists attendance_pkey;
-- alter table attendance add primary key (placeholder, day, session);

