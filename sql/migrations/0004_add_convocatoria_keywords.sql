-- Run once in the Supabase SQL editor against the live database.
-- Narrows the convocatorias source (scripts/convocatorias_client.py +
-- sync.py) from scraping the entire /convocatorias/ catalog (~191 pages,
-- ~3,800 listings) to only the keywords listed here, using the site's own
-- server-side search (?q=kw1,kw2, OR semantics). sync.py's
-- run_convocatorias() reads the active keywords from this table on every
-- run before fetching.
--
-- Existing rows in convocatorias_dim/convocatorias_snapshot from the prior
-- unfiltered scrapes are left alone by this migration. The existing
-- diff_and_upsert_dim() logic will naturally flip anything that stops
-- appearing in the now-filtered fetch to is_active=false / event_type
-- 'closed' on the next run — that's expected, not a bug, and 'closed'
-- events don't trigger Telegram notifications.

create table convocatoria_keywords (
  keyword text primary key,
  is_active boolean not null default true,
  created_at timestamptz not null default now()
);

insert into convocatoria_keywords (keyword, is_active) values
  ('Biologo', true),
  ('Biologia', true);
