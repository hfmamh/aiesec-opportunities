-- Run once in the Supabase SQL editor against the live database.
-- Moves the Telegram destination chat out of a single shared GitHub secret
-- and into the database, keyed by source, so each source's notifications can
-- go to a different chat (and adding a source's channel is a data change,
-- not a code/secret change).

create table notification_channels (
  source text primary key,
  chat_id text not null
);

-- Seed with your real chat_id values before the next sync run, e.g.:
-- insert into notification_channels (source, chat_id) values
--   ('aiesec', '<chat id>'),
--   ('convocatorias', '<chat id>');
