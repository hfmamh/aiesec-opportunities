-- Run this once in the Supabase SQL editor for a fresh project.

create table opportunities_snapshot (
  run_date date not null,
  id text not null,
  title text,
  location text,
  country text,
  company text,
  primary key (run_date, id)
);

create table opportunities_dim (
  id text primary key,
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  is_active boolean not null default true,
  times_seen integer not null default 1,
  current_title text,
  current_location text,
  current_country text,
  current_company text
);

create table opportunity_events (
  event_id bigint generated always as identity primary key,
  opportunity_id text not null,
  event_type text not null check (event_type in ('created', 'updated', 'closed', 'reopened')),
  event_at timestamptz not null default now(),
  field text,
  old_value text,
  new_value text
);

create table ingestion_runs (
  run_id bigint generated always as identity primary key,
  run_at timestamptz not null default now(),
  status text not null check (status in ('running', 'success', 'failed')),
  pages_fetched integer,
  items_fetched integer,
  duration_ms integer,
  error text
);

create index on opportunities_snapshot (id);
create index on opportunity_events (opportunity_id);

-- Also create a Storage bucket named "raw-snapshots" (Storage tab in the
-- Supabase dashboard, or SQL: select storage.create_bucket('raw-snapshots')
-- depending on your project version) to hold one raw JSON file per run.
