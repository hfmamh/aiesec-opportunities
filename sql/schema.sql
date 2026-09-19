-- Run this once in the Supabase SQL editor for a fresh project.

-- NOTE: this file reflects the current full schema for a fresh install.
-- Changes to an already-running database go through sql/migrations/ instead
-- (see that directory's README note) and get mirrored here afterwards.

-- ============================================================
-- Source: AIESEC (scripts/aiesec_client.py)
-- ============================================================

create table opportunities_snapshot (
  run_date date not null,
  id text not null,
  title text,
  location text,
  country text,
  company text,
  salary numeric,
  salary_currency text,
  salary_periodicity text,
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
  current_company text,
  current_salary numeric,
  current_salary_currency text,
  current_salary_periodicity text
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

create index on opportunities_snapshot (id);
create index on opportunity_events (opportunity_id);

-- ============================================================
-- Source: convocatorias del Estado peruano (scripts/convocatorias_client.py)
-- Scraped from convocatoriasestado.pe/convocatorias/ (itself sourced from
-- SERVIR's Talento Perú portal, which doesn't keep history).
-- ============================================================

create table convocatorias_snapshot (
  run_date date not null,
  id text not null,
  titulo text,
  entidad text,
  departamento text,
  distrito text,
  vacantes integer,
  sueldo numeric,
  modalidad text,
  tiene_bases boolean,
  fecha_cierre date,
  url text,
  primary key (run_date, id)
);

create table convocatorias_dim (
  id text primary key,
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  is_active boolean not null default true,
  times_seen integer not null default 1,
  current_titulo text,
  current_entidad text,
  current_departamento text,
  current_distrito text,
  current_vacantes integer,
  current_sueldo numeric,
  current_modalidad text,
  current_tiene_bases boolean,
  current_fecha_cierre date,
  current_url text
);

create table convocatoria_events (
  event_id bigint generated always as identity primary key,
  convocatoria_id text not null,
  event_type text not null check (event_type in ('created', 'updated', 'closed', 'reopened')),
  event_at timestamptz not null default now(),
  field text,
  old_value text,
  new_value text
);

create index on convocatorias_snapshot (id);
create index on convocatoria_events (convocatoria_id);

-- ============================================================
-- Shared across sources
-- ============================================================

create table ingestion_runs (
  run_id bigint generated always as identity primary key,
  run_at timestamptz not null default now(),
  source text not null default 'aiesec',
  status text not null check (status in ('running', 'success', 'failed')),
  pages_fetched integer,
  items_fetched integer,
  duration_ms integer,
  error text
);

create index on ingestion_runs (source, run_at);

-- Also create a Storage bucket named "raw-snapshots" (Storage tab in the
-- Supabase dashboard, or SQL: select storage.create_bucket('raw-snapshots')
-- depending on your project version). Each source archives its raw fetch
-- under its own prefix: raw-snapshots/aiesec/{date}.json and
-- raw-snapshots/convocatorias/{date}.json.
