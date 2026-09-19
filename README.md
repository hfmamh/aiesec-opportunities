# Opportunities & Convocatorias Pipeline

Daily, cloud-hosted pipeline that tracks two job-listing sources, keeps a
full history of when items appear/change/close, and feeds a Looker Studio
dashboard. Runs on GitHub Actions; storage is Supabase (Postgres + object
storage). No server, no local machine dependency, $0/month at
personal-project scale.

- **AIESEC**: fetches AIESEC's GraphQL opportunities feed.
- **Convocatorias del Estado peruano**: scrapes the public listing pages at
  [convocatoriasestado.pe](https://convocatoriasestado.pe/convocatorias/),
  which itself aggregates SERVIR's official Talento Perú portal (the
  original source) and adds the history SERVIR doesn't keep. SERVIR's own
  site (`app.servir.gob.pe`) sits behind an Incapsula bot-protection layer
  and would need full browser automation to scrape directly; this site is
  plain server-rendered HTML with `robots.txt` explicitly allowing
  `/convocatorias/`, and has a paid API for higher-volume use that wasn't in
  budget for this project — so this pipeline scrapes its HTML instead, at a
  polite request rate (one page every 0.5s, ~95s for the full ~190-page
  catalog per run).

Both sources follow the same pattern: append-only daily snapshot, a "dim"
table with each item's current state, and an events log of
created/updated/closed/reopened — so nothing is ever overwritten and you can
always answer "when did this first appear" or "when did it close".

## How it works

- `.github/workflows/sync.yml` runs `scripts/sync.py` every 4 hours (and
  on-demand via the Actions tab "Run workflow" button).
- `scripts/sync.py`'s `main()` runs both sources independently — one
  failing (e.g. AIESEC's key rotated, or the scraped site's markup changed)
  never blocks the other, and each is logged as its own row in
  `ingestion_runs` (tagged by `source`).
- For each source, a run:
  1. Logs a run in `ingestion_runs`.
  2. Archives the raw fetched data to the Supabase Storage bucket
     `raw-snapshots/{source}/{date}.json`.
  3. Writes today's flattened rows to `opportunities_snapshot` /
     `convocatorias_snapshot` (append-only, one row per item per day).
  4. Diffs against `opportunities_dim` / `convocatorias_dim` (current state
     per item) and records `created` / `updated` / `closed` / `reopened`
     rows in `opportunity_events` / `convocatoria_events`.
  5. Marks the run `success` or `failed` with counts/duration/error.
  6. Sends one grouped Telegram message for that run's newly created items
     (best-effort — a notification failure is logged but doesn't fail the
     run, since the data sync already succeeded).
- `scripts/aiesec_client.py` pages through the full GraphQL result set for
  today's opportunities.
- `scripts/convocatorias_client.py` pages through
  `convocatoriasestado.pe/convocatorias/?page=N` and parses each listing
  card's HTML.

See `sql/schema.sql` for the full table definitions.

## One-time setup

### 1. Supabase project

1. Create a free project at supabase.com.
2. Open the SQL editor and run the contents of `sql/schema.sql`.
3. Go to Storage and create a new bucket named `raw-snapshots` (private is
   fine).
4. Go to Project Settings -> API and copy the **Project URL** and the
   **service_role** key (not the anon key — the sync job needs to bypass
   row-level security since it writes server-side).

### 2. GitHub repo secrets

In the repo, go to Settings -> Secrets and variables -> Actions and add:

| Secret | Value |
|---|---|
| `AIESEC_API_KEY` | The embedded public key from `aiesec_opportunities_table.py` |
| `SUPABASE_URL` | Project URL from step 1 |
| `SUPABASE_SERVICE_ROLE_KEY` | service_role key from step 1 |
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |

`convocatorias_client.py` needs no credentials — it scrapes a public page.

### 3. Notification channels

Which Telegram chat each source notifies is a **database row**, not a
secret — this is what lets each source post to a different chat, and what
makes adding a new source's channel a data change instead of a code change.
For each source, insert a row into `notification_channels` (SQL editor):

```sql
insert into notification_channels (source, chat_id) values
  ('aiesec', '<chat id>'),
  ('convocatorias', '<chat id>');
```

To get a chat's `chat_id`: send any message in that chat (a DM to the bot,
or a group/channel the bot has been added to), then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read the
`"chat": {"id": ...}` field of the most recent update. If a source has no
row here, its notification step fails loudly (logged, doesn't fail the
sync) rather than silently going nowhere.

### 4. Looker Studio dashboard

1. In Supabase, go to Project Settings -> Database and copy the connection
   pooler host/port/database/user/password.
2. In Looker Studio, create a new data source using the built-in
   **PostgreSQL** connector and those credentials.
3. Add the snapshot/dim/events tables for whichever source(s) you want, then
   build charts, e.g.:
   - Active items over time: `count(*)` grouped by `run_date`.
   - New vs. closed per day: `count(*)` from the events table grouped by
     `date(event_at)` and `event_type`.
   - AIESEC breakdown by country/company, or convocatorias breakdown by
     departamento/entidad: `count(*)` from the dim table where `is_active`.
   - Average lifetime: average of `(closed event_at - created event_at)` per
     item, joining the events table on itself by id.

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # fill in the values you have
python scripts\sync.py
```

## Adding a new tracked field

Fields are declared once in each source's `FIELD_EXTRACTORS` registry
(`scripts/aiesec_client.py` / `scripts/convocatorias_client.py`) and flow
through the whole pipeline automatically — the snapshot table, the dim
table, and the created/updated/closed diff logic in `scripts/sync.py` all
key off that registry, not a field list you have to update in multiple
places.

- **AIESEC**: add the field to the `QUERY` string, then add one entry to
  `FIELD_EXTRACTORS` mapping a column name to a function that pulls it out
  of the raw GraphQL `op` object (use the `_dig` helper for nested fields).
- **Convocatorias**: make sure `_parse_article` captures the raw string for
  it (add a regex if it's a new bit of markup), then add one entry to
  `FIELD_EXTRACTORS` mapping a column name to a function that turns that raw
  string into a typed value.

Either way, you'll still need to add the matching database columns: a plain
column on the snapshot table, and a `current_<field>` column on the dim
table.
- **Fresh install**: add the columns directly to `sql/schema.sql`.
- **Existing/live database**: add a new numbered file under
  `sql/migrations/` (e.g. `0003_add_whatever.sql`) with the `ALTER TABLE`
  statements, run it once in the Supabase SQL editor, and mirror the same
  columns into `sql/schema.sql` so a fresh install matches.

Nothing else needs to change — `sync.py` derives each source's
`TRACKED_FIELDS` from its `FIELD_EXTRACTORS`, so the new field is
automatically snapshotted daily and tracked for created/updated/closed
events.

## Rotating the AIESEC API key

If the AIESEC side starts failing with 401/402/403s, the embedded public key
has likely changed. Recapture it from a browser: DevTools -> Network ->
request to `gis-api.aiesec.org` -> Headers -> `authorization`, then update
the `AIESEC_API_KEY` GitHub secret (and your local `.env`).

## If convocatoriasestado.pe's markup changes

`convocatorias_client.py`'s regexes are tied to that site's current HTML
(class names like `card`, `card-entidad`, `sueldo`, `cierre`, and the icon
hrefs `#i-pin` / `#i-vacantes` / `#i-maletin`). If that source's run starts
returning 0 rows, or every field comes back `None`, the fastest way to fix
it is to open `/convocatorias/` in a browser, inspect one `<article
class="card">` element, and adjust the regexes in `_parse_article` and the
module-level `*_RE` patterns to match the new markup.
