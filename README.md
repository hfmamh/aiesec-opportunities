# AIESEC Opportunities Pipeline

Daily, cloud-hosted pipeline that fetches AIESEC's GraphQL opportunities feed,
keeps a full history of when opportunities appear/change/close, and feeds a
Looker Studio dashboard. Runs on GitHub Actions; storage is Supabase
(Postgres + object storage). No server, no local machine dependency, $0/month
at personal-project scale.

## How it works

- `.github/workflows/sync.yml` runs `scripts/sync.py` daily at 06:00 UTC (and
  on-demand via the Actions tab "Run workflow" button).
- `scripts/aiesec_client.py` pages through the full GraphQL result set for
  today's opportunities.
- `scripts/sync.py`:
  1. Logs a run in `ingestion_runs`.
  2. Archives the raw API responses to the Supabase Storage bucket
     `raw-snapshots/{date}.json`.
  3. Writes today's flattened rows to `opportunities_snapshot` (append-only,
     one row per opportunity per day).
  4. Diffs against `opportunities_dim` (current state per opportunity) and
     records `created` / `updated` / `closed` / `reopened` rows in
     `opportunity_events`.
  5. Marks the run `success` or `failed` with counts/duration/error.

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

### 3. Looker Studio dashboard

1. In Supabase, go to Project Settings -> Database and copy the connection
   pooler host/port/database/user/password.
2. In Looker Studio, create a new data source using the built-in
   **PostgreSQL** connector and those credentials.
3. Add `opportunities_snapshot`, `opportunities_dim`, and
   `opportunity_events` as tables/custom queries, then build charts, e.g.:
   - Active opportunities over time: `count(*)` from `opportunities_snapshot`
     grouped by `run_date`.
   - New vs. closed per day: `count(*)` from `opportunity_events` grouped by
     `date(event_at)` and `event_type`.
   - Breakdown by country/company: `count(*)` from `opportunities_dim` where
     `is_active` grouped by `current_country` / `current_company`.
   - Average lifetime: average of `(closed event_at - created event_at)` per
     opportunity, joining `opportunity_events` on itself by `opportunity_id`.

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # fill in the three values
python scripts\sync.py
```

## Rotating the AIESEC API key

If the pipeline starts failing with 401/402/403s, the embedded public key
has likely changed. Recapture it from a browser: DevTools -> Network ->
request to `gis-api.aiesec.org` -> Headers -> `authorization`, then update
the `AIESEC_API_KEY` GitHub secret (and your local `.env`).
