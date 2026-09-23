-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run

create table if not exists participants (
  placeholder text primary key,
  roll_no text,
  name text
);

create table if not exists attendance (
  placeholder text not null references participants(placeholder),
  day integer not null,
  marked_at timestamptz not null default now(),
  marked_by text not null,
  primary key (placeholder, day)
);

create table if not exists scan_log (
  id bigint generated always as identity primary key,
  ts timestamptz not null default now(),
  code text,
  result text not null,
  volunteer text
);

-- pre-insert all 80 placeholders so admin/manual-mark lookups work
-- even before participants.csv is filled in and loaded
insert into participants (placeholder)
select 'P' || lpad(i::text, 3, '0')
from generate_series(1, 80) as i
on conflict (placeholder) do nothing;
