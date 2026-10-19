-- Run once in the Supabase SQL editor against the live database.
-- Adds per-chat state for the telegram-resumen Edge Function now that it
-- talks to all three sources (aiesec/convocatorias/getonboard) instead of
-- only convocatorias: a chat must pick one with /usar_aiesec,
-- /usar_convocatorias or /usar_getonboard before /resumen or a free-form
-- question will work, and that choice persists (per chat_id) until changed.

create table telegram_bot_sessions (
  chat_id text primary key,
  active_source text not null check (active_source in ('aiesec', 'convocatorias', 'getonboard')),
  updated_at timestamptz not null default now()
);
