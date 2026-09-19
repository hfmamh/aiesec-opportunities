-- Run once in the Supabase SQL editor against the live database.
-- Adds the second pipeline: convocatorias del Estado peruano, scraped from
-- convocatoriasestado.pe (scripts/convocatorias_client.py + sync.py).

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

-- Existing rows get 'aiesec' via the default, matching what they always
-- were; new runs of either source now write an explicit source value.
alter table ingestion_runs
  add column source text not null default 'aiesec';

create index on ingestion_runs (source, run_at);

-- Also create a Storage bucket named "raw-snapshots" if it doesn't already
-- exist (it does, from the AIESEC pipeline's setup) — no action needed,
-- convocatorias just archives under its own raw-snapshots/convocatorias/
-- prefix in the same bucket.
