-- Run once in the Supabase SQL editor against the live database.
-- Splits the convocatorias source into topics: each topic has its own
-- keywords and its own Telegram chat, so a new topic (and channel) is a data
-- change, not a code change. sync.py's run_convocatorias() runs one search
-- per active topic, diffs the union into the single convocatorias_dim as
-- before, and notifies each topic's chat about listings that are new *for
-- that topic* (tracked in convocatoria_topic_matches), so a listing that
-- matches two topics reaches both chats, and never the same chat twice.

-- 1. Topics: one row per Telegram channel.
create table convocatoria_topics (
  slug text primary key,
  nombre text not null,
  chat_id text not null,
  is_active boolean not null default true,
  -- false (default): a brand-new topic's first run records everything it
  -- currently matches without notifying, so creating a topic doesn't flood
  -- its chat with every open listing. true: send them all on that run.
  notify_first_run boolean not null default false,
  created_at timestamptz not null default now()
);

-- 2. Existing setup becomes the 'biologia' topic, reusing the chat that
-- notification_channels had for source='convocatorias'.
insert into convocatoria_topics (slug, nombre, chat_id)
select 'biologia', 'Biología', chat_id
from notification_channels
where source = 'convocatorias';

-- 3. Keywords now belong to a topic. The same keyword may appear in more
-- than one topic, so the primary key becomes (topic_slug, keyword).
alter table convocatoria_keywords add column topic_slug text references convocatoria_topics(slug);
update convocatoria_keywords set topic_slug = 'biologia' where topic_slug is null;
alter table convocatoria_keywords alter column topic_slug set not null;
alter table convocatoria_keywords drop constraint convocatoria_keywords_pkey;
alter table convocatoria_keywords add primary key (topic_slug, keyword);

-- 4. Which listing has already been notified to which topic.
create table convocatoria_topic_matches (
  convocatoria_id text not null,
  topic_slug text not null references convocatoria_topics(slug),
  matched_at timestamptz not null default now(),
  primary key (convocatoria_id, topic_slug)
);

-- Everything already in convocatorias_dim was found via the biology
-- keywords (or the earlier unfiltered scrape), so mark it all as already
-- seen by 'biologia'. Without this, the first run after this migration
-- would re-notify every currently open biology listing.
insert into convocatoria_topic_matches (convocatoria_id, topic_slug)
select id, 'biologia' from convocatorias_dim;

-- notification_channels' 'convocatorias' row is no longer read (each topic
-- carries its own chat_id); it's left in place, harmless.

-- 5. New topic: Ingeniería Ambiental. Create a Telegram group, add the bot,
-- get its chat_id (see README, "Notification channels"), then replace the
-- placeholder and run:
--
-- insert into convocatoria_topics (slug, nombre, chat_id) values
--   ('ambiental', 'Ingeniería Ambiental', '<chat id>');
-- insert into convocatoria_keywords (topic_slug, keyword, is_active) values
--   ('ambiental', 'Ingeniería Ambiental', true);
