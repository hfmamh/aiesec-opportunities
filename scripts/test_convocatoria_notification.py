"""Manual test: scrapes page 1 of convocatoriasestado.pe for one topic's
keywords (or TEST_KEYWORD if set), picks the first listing, and sends it
to that topic's Telegram chat through the real notification code.

TEST_TOPIC is the convocatoria_topics.slug to test (e.g. 'biologia',
'ambiental'); it may be inactive, so a topic can be tried before it goes
live.

Read-only against the database: it only reads convocatoria_topics and
convocatoria_keywords. Nothing is written to snapshot/dim/events tables,
convocatoria_topic_matches, ingestion_runs, or Storage, so the regular sync
is unaffected.
"""

import os

import convocatorias_client
import notifier
from supabase_client import get_client


def main():
    client = get_client()

    slug = os.environ.get("TEST_TOPIC", "").strip()
    if not slug:
        raise RuntimeError("TEST_TOPIC is not set")
    topics = client.table("convocatoria_topics").select("*").eq("slug", slug).execute().data
    if not topics:
        raise RuntimeError(f"No convocatoria_topics row for slug={slug!r}")
    topic = topics[0]

    keyword = os.environ.get("TEST_KEYWORD", "").strip()
    if keyword:
        keywords = [keyword]
    else:
        rows = client.table("convocatoria_keywords").select("keyword").eq("topic_slug", slug).eq("is_active", True).execute().data
        keywords = [row["keyword"] for row in rows]
    if not keywords:
        raise RuntimeError(f"Topic {slug!r} has no active keywords; pass TEST_KEYWORD")
    print(f"Topic {slug!r}, searching with keywords: {keywords}")

    html_text = convocatorias_client.fetch_page(1, keywords=keywords)
    if html_text is None:
        raise RuntimeError("No results page returned")

    for chunk in convocatorias_client.ARTICLE_RE.findall(html_text):
        raw = convocatorias_client._parse_article(chunk)
        if raw is not None:
            break
    else:
        raise RuntimeError("No convocatorias found on page 1")

    row = convocatorias_client.to_rows([raw])[0]
    print(f"Picked convocatoria: {row}")

    notifier.notify_convocatoria_topic(topic["chat_id"], topic["nombre"], [row])
    print("Sent test notification")


if __name__ == "__main__":
    main()
