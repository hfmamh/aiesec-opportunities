import datetime
import json
import sys
import time

import aiesec_client
import convocatorias_client
import getonboard_client
import notifier
from supabase_client import get_client

AIESEC_TRACKED_FIELDS = list(aiesec_client.FIELD_EXTRACTORS.keys())
CONVOCATORIAS_TRACKED_FIELDS = list(convocatorias_client.FIELD_EXTRACTORS.keys())
GETONBOARD_TRACKED_FIELDS = list(getonboard_client.FIELD_EXTRACTORS.keys())


def start_run(client, source):
    resp = client.table("ingestion_runs").insert({"status": "running", "source": source}).execute()
    return resp.data[0]["run_id"]


def finish_run(client, run_id, status, **fields):
    client.table("ingestion_runs").update({"status": status, **fields}).eq("run_id", run_id).execute()


def archive_raw(client, archive_prefix, today, raw_pages):
    payload = json.dumps(raw_pages).encode("utf-8")
    client.storage.from_("raw-snapshots").upload(
        f"{archive_prefix}/{today}.json",
        payload,
        {"content-type": "application/json", "upsert": "true"},
    )


def upsert_snapshot(client, snapshot_table, today, rows):
    if not rows:
        return
    records = [{"run_date": today, **row} for row in rows]
    client.table(snapshot_table).upsert(records, on_conflict="run_date,id").execute()


DIM_PAGE_SIZE = 1000  # Supabase's REST API caps a single select() at 1000 rows


def fetch_all_rows(client, table):
    """select("*") on a Supabase table, paginated past the API's 1000-row
    cap. Without this, any table over 1000 rows would silently only return
    its first page."""
    rows = []
    start = 0
    while True:
        page = client.table(table).select("*").range(start, start + DIM_PAGE_SIZE - 1).execute().data
        rows.extend(page)
        if len(page) < DIM_PAGE_SIZE:
            break
        start += DIM_PAGE_SIZE
    return rows


def diff_and_upsert_dim(client, dim_table, events_table, event_id_column, tracked_fields, today_iso, rows):
    """Compares today's fetch against `dim_table`, applies changes, and
    returns (created_count, updated_count, closed_count, reopened_count,
    created_rows). created_rows is the list of full today-row dicts for the
    items that were newly created this run (used for notifications).

    Source-agnostic: works for any table shaped like opportunities_dim /
    convocatorias_dim (id, first_seen_at, last_seen_at, is_active,
    times_seen, current_<field> per tracked field) and any events table
    shaped like opportunity_events / convocatoria_events (using
    `event_id_column` as the foreign-key column name, e.g. "opportunity_id"
    or "convocatoria_id")."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    today_by_id = {row["id"]: row for row in rows}

    existing = fetch_all_rows(client, dim_table)
    existing_by_id = {row["id"]: row for row in existing}

    dim_upserts = []
    events = []
    created_rows = []
    created = updated = closed = reopened = 0

    for item_id, row in today_by_id.items():
        prior = existing_by_id.get(item_id)

        if prior is None:
            new_record = {
                "id": item_id,
                "first_seen_at": now,
                "last_seen_at": now,
                "is_active": True,
                "times_seen": 1,
            }
            for field in tracked_fields:
                new_record[f"current_{field}"] = row[field]
            dim_upserts.append(new_record)
            events.append({event_id_column: item_id, "event_type": "created"})
            created_rows.append(row)
            created += 1
            continue

        record = {
            "id": item_id,
            "first_seen_at": prior["first_seen_at"],
            "last_seen_at": now,
            "times_seen": prior["times_seen"] + 1,
            "is_active": True,
        }

        any_field_changed = False
        for field in tracked_fields:
            old_value = prior.get(f"current_{field}")
            new_value = row[field]
            record[f"current_{field}"] = new_value
            if old_value != new_value:
                any_field_changed = True
                events.append({
                    event_id_column: item_id,
                    "event_type": "updated",
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_value,
                })

        if not prior["is_active"]:
            events.append({event_id_column: item_id, "event_type": "reopened"})
            reopened += 1
        elif any_field_changed:
            updated += 1

        dim_upserts.append(record)

    for item_id, prior in existing_by_id.items():
        if prior["is_active"] and item_id not in today_by_id:
            closed_record = {
                "id": item_id,
                "first_seen_at": prior["first_seen_at"],
                "last_seen_at": prior["last_seen_at"],
                "times_seen": prior["times_seen"],
                "is_active": False,
            }
            for field in tracked_fields:
                closed_record[f"current_{field}"] = prior[f"current_{field}"]
            dim_upserts.append(closed_record)
            events.append({event_id_column: item_id, "event_type": "closed"})
            closed += 1

    if dim_upserts:
        client.table(dim_table).upsert(dim_upserts, on_conflict="id").execute()
    if events:
        client.table(events_table).insert(events).execute()

    return created, updated, closed, reopened, created_rows


def run_aiesec(client):
    """Fetches, archives, snapshots and diffs today's AIESEC opportunities.
    Returns (rows_count, pages_count, created_rows). Raises on any failure;
    the caller is responsible for logging it to ingestion_runs."""
    today = datetime.date.today().isoformat()
    raw_pages, rows = aiesec_client.fetch_all(earliest_start_date=today)

    archive_raw(client, "aiesec", today, raw_pages)
    upsert_snapshot(client, "opportunities_snapshot", today, rows)
    created, updated, closed, reopened, created_rows = diff_and_upsert_dim(
        client, "opportunities_dim", "opportunity_events", "opportunity_id",
        AIESEC_TRACKED_FIELDS, today, rows,
    )
    print(
        f"[aiesec] OK: {len(rows)} opportunities across {len(raw_pages)} pages "
        f"(+{created} created, {updated} updated, {closed} closed, {reopened} reopened)"
    )
    return len(rows), len(raw_pages), created_rows


def get_active_topics(client):
    """Returns the active convocatoria_topics rows, each with a "keywords"
    list of its active convocatoria_keywords. A topic with no active
    keywords is skipped with a warning (it has nothing to search for).
    Paginated defensively via fetch_all_rows even though neither table will
    realistically exceed 1000 rows."""
    keywords_by_topic = {}
    for row in fetch_all_rows(client, "convocatoria_keywords"):
        if row.get("is_active"):
            keywords_by_topic.setdefault(row["topic_slug"], []).append(row["keyword"])

    topics = []
    for topic in fetch_all_rows(client, "convocatoria_topics"):
        if not topic.get("is_active"):
            continue
        keywords = keywords_by_topic.get(topic["slug"])
        if not keywords:
            print(
                f"[convocatorias] WARNING: topic {topic['slug']!r} has no active keywords; skipping it",
                file=sys.stderr,
            )
            continue
        topics.append({**topic, "keywords": keywords})
    return topics


def record_topic_matches(client, topics, ids_by_topic, rows_by_id):
    """Records which of today's listings are new *for each topic* in
    convocatoria_topic_matches and returns the notifications to send: a list
    of {"chat_id", "nombre", "rows"}, one per topic with new listings.

    A topic with no prior matches at all is on its first run: unless its
    notify_first_run is set, its matches are recorded silently, so creating
    a topic doesn't flood its chat with every listing already open."""
    seen_by_topic = {}
    for row in fetch_all_rows(client, "convocatoria_topic_matches"):
        seen_by_topic.setdefault(row["topic_slug"], set()).add(row["convocatoria_id"])

    inserts = []
    notifications = []
    for topic in topics:
        slug = topic["slug"]
        seen = seen_by_topic.get(slug, set())
        new_ids = [item_id for item_id in ids_by_topic[slug] if item_id not in seen]
        if not new_ids:
            continue

        inserts.extend({"convocatoria_id": item_id, "topic_slug": slug} for item_id in new_ids)
        if not seen and not topic.get("notify_first_run"):
            print(f"[convocatorias] topic {slug!r} first run: recorded {len(new_ids)} matches without notifying")
            continue
        notifications.append({
            "chat_id": topic["chat_id"],
            "nombre": topic["nombre"],
            "rows": [rows_by_id[item_id] for item_id in new_ids],
        })

    if inserts:
        client.table("convocatoria_topic_matches").upsert(
            inserts, on_conflict="convocatoria_id,topic_slug"
        ).execute()
    return notifications


def run_convocatorias(client):
    """Fetches, archives, snapshots and diffs today's convocatorias from
    convocatoriasestado.pe, running one server-side search per active
    topic in `convocatoria_topics` (with that topic's keywords). The union
    of all topics' listings is what gets snapshotted and diffed into the
    single convocatorias_dim. Returns (rows_count, pages_count,
    notifications) where notifications comes from record_topic_matches
    (per-topic "new for this topic", not dim's "created").

    If any topic's fetch fails the whole run fails: diffing a partial union
    would wrongly mark that topic's listings as closed.

    If there are zero active topics, this deliberately skips the fetch
    (rather than falling back to the full unfiltered catalog, which would
    silently re-trigger a ~191-page scrape) and reports 0 items fetched as
    a successful run."""
    today = datetime.date.today().isoformat()

    topics = get_active_topics(client)
    if not topics:
        print(
            "[convocatorias] WARNING: no active topics with keywords; "
            "skipping fetch (0 items) rather than scraping the full catalog",
            file=sys.stderr,
        )

    raw_pages = []
    rows_by_id = {}
    ids_by_topic = {}
    for topic in topics:
        topic_pages, topic_rows = convocatorias_client.fetch_all(keywords=topic["keywords"])
        raw_pages.extend({"topic": topic["slug"], **page} for page in topic_pages)
        ids_by_topic[topic["slug"]] = list(dict.fromkeys(row["id"] for row in topic_rows))
        for row in topic_rows:
            rows_by_id[row["id"]] = row
        print(f"[convocatorias] topic {topic['slug']!r}: {len(topic_rows)} listings")
    rows = list(rows_by_id.values())

    archive_raw(client, "convocatorias", today, raw_pages)
    upsert_snapshot(client, "convocatorias_snapshot", today, rows)
    created, updated, closed, reopened, _ = diff_and_upsert_dim(
        client, "convocatorias_dim", "convocatoria_events", "convocatoria_id",
        CONVOCATORIAS_TRACKED_FIELDS, today, rows,
    )
    notifications = record_topic_matches(client, topics, ids_by_topic, rows_by_id)
    print(
        f"[convocatorias] OK: {len(rows)} convocatorias across {len(raw_pages)} pages "
        f"(+{created} created, {updated} updated, {closed} closed, {reopened} reopened)"
    )
    return len(rows), len(raw_pages), notifications


def run_getonboard(client):
    """Fetches, archives, snapshots and diffs today's Get on Board listings
    for the "data-engineer" tag. Same return/raise contract as run_aiesec."""
    today = datetime.date.today().isoformat()
    raw_pages, rows = getonboard_client.fetch_all()

    archive_raw(client, "getonboard", today, raw_pages)
    upsert_snapshot(client, "getonboard_snapshot", today, rows)
    created, updated, closed, reopened, created_rows = diff_and_upsert_dim(
        client, "getonboard_dim", "getonboard_events", "getonboard_id",
        GETONBOARD_TRACKED_FIELDS, today, rows,
    )
    print(
        f"[getonboard] OK: {len(rows)} listings across {len(raw_pages)} pages "
        f"(+{created} created, {updated} updated, {closed} closed, {reopened} reopened)"
    )
    return len(rows), len(raw_pages), created_rows


def run_source(client, source, run_fn, notify_fn):
    """Wraps one source's run_fn with ingestion_runs bookkeeping and a
    best-effort Telegram notification. run_fn's third return value is
    whatever that source's notify_fn takes (created rows for most sources,
    per-topic notifications for convocatorias); it's skipped when empty.
    A failure here is caught and reported by the caller (main), so one
    source failing never stops the other."""
    started = time.monotonic()
    run_id = start_run(client, source)
    try:
        items_fetched, pages_fetched, to_notify = run_fn(client)
        duration_ms = int((time.monotonic() - started) * 1000)
        finish_run(
            client, run_id, "success",
            pages_fetched=pages_fetched, items_fetched=items_fetched, duration_ms=duration_ms,
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        finish_run(client, run_id, "failed", duration_ms=duration_ms, error=str(exc)[:2000])
        raise

    # Ingestion already succeeded and was recorded at this point. A
    # notify_fn failure (missing secrets, API hiccup, etc.) is logged but
    # must not be treated as a pipeline failure, since the data sync
    # already worked.
    if to_notify:
        try:
            notify_fn(client, to_notify)
            print(f"[{source}] Ran notify_fn")
        except Exception as exc:
            print(f"[{source}] WARNING: notify_fn failed: {exc}", file=sys.stderr)


def main():
    client = get_client()

    sources = [
        ("aiesec", run_aiesec, notifier.notify_new_opportunities),
        ("convocatorias", run_convocatorias, notifier.notify_convocatoria_topics),
        ("getonboard", run_getonboard, notifier.notify_new_getonboard),
    ]

    failures = []
    for source, run_fn, notify_fn in sources:
        try:
            run_source(client, source, run_fn, notify_fn)
        except Exception as exc:
            print(f"[{source}] FAILED: {exc}", file=sys.stderr)
            failures.append(source)

    if failures:
        print(f"One or more sources failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
