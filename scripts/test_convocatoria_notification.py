"""Manual test: scrapes page 1 of convocatoriasestado.pe (narrowed to the
active keywords, or to TEST_KEYWORD if set), picks the first listing, and
sends it through the real convocatorias Telegram notification.

Read-only against the database: it only reads convocatoria_keywords and
notification_channels. Nothing is written to snapshot/dim/events tables,
ingestion_runs, or Storage, so the regular sync is unaffected.
"""

import os

import convocatorias_client
import notifier
from supabase_client import get_client
from sync import get_active_keywords


def main():
    client = get_client()

    keyword = os.environ.get("TEST_KEYWORD", "").strip()
    keywords = [keyword] if keyword else get_active_keywords(client)
    print(f"Searching with keywords: {keywords or '(none: full catalog)'}")

    html_text = convocatorias_client.fetch_page(1, keywords=keywords or None)
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

    notifier.notify_new_convocatorias(client, [row])
    print("Sent test notification")


if __name__ == "__main__":
    main()
