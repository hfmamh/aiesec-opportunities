-- Run once in the Supabase SQL editor against the live database.
-- Adds the third pipeline: Get on Board (getonbrd.com.pe), scraped from its
-- public job-tag listing page (scripts/getonboard_client.py + sync.py).
--
-- Unlike convocatorias, there's no numeric id anywhere in the listing HTML,
-- so `id` here is the job's URL path (e.g.
-- "/jobs/data-science-analytics/data-engineer-codeable-lima"), which is
-- stable across runs. See getonboard_client.py's module docstring for why
-- the id deliberately excludes scheme/host/port.
--
-- Only one tag is fetched for now ("data-engineer", hardcoded in
-- getonboard_client.py), not a configurable keywords table like
-- convocatoria_keywords -- revisit that if/when more tags are needed.

create table getonboard_snapshot (
  run_date date not null,
  id text not null,
  titulo text,
  empresa text,
  ubicacion text,
  tipo_empleo text,
  categoria text,
  salario text,
  fecha_publicacion date,
  url text,
  primary key (run_date, id)
);

create table getonboard_dim (
  id text primary key,
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  is_active boolean not null default true,
  times_seen integer not null default 1,
  current_titulo text,
  current_empresa text,
  current_ubicacion text,
  current_tipo_empleo text,
  current_categoria text,
  current_salario text,
  current_fecha_publicacion date,
  current_url text
);

create table getonboard_events (
  event_id bigint generated always as identity primary key,
  getonboard_id text not null,
  event_type text not null check (event_type in ('created', 'updated', 'closed', 'reopened')),
  event_at timestamptz not null default now(),
  field text,
  old_value text,
  new_value text
);

create index on getonboard_snapshot (id);
create index on getonboard_events (getonboard_id);

-- ingestion_runs.source and notification_channels.source are both plain
-- text columns with no check constraint (see 0002/0003), so no migration
-- is needed there -- sync.py just starts writing rows with
-- source='getonboard'. A notification_channels row for 'getonboard' still
-- needs to be inserted by whoever owns notifications before that source's
-- Telegram notifications will work; until then, created items are detected
-- and stored correctly, but a missing-channel error is logged (not raised)
-- the same way it would be for any source with no notification_channels
-- row.
--
-- Also create a Storage bucket named "raw-snapshots" if it doesn't already
-- exist (it does, from the AIESEC pipeline's setup) -- getonboard just
-- archives under its own raw-snapshots/getonboard/ prefix in the same
-- bucket.
