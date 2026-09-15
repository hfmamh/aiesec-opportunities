import datetime
import json
import sys
import time

from aiesec_client import FIELD_EXTRACTORS, fetch_all
from supabase_client import get_client

TRACKED_FIELDS = list(FIELD_EXTRACTORS.keys())


def start_run(client):
    resp = client.table("ingestion_runs").insert({"status": "running"}).execute()
    return resp.data[0]["run_id"]


def finish_run(client, run_id, status, **fields):
    client.table("ingestion_runs").update({"status": status, **fields}).eq("run_id", run_id).execute()


def archive_raw(client, today, raw_pages):
    payload = json.dumps(raw_pages).encode("utf-8")
    client.storage.from_("raw-snapshots").upload(
        f"{today}.json",
        payload,
        {"content-type": "application/json", "upsert": "true"},
    )


def upsert_snapshot(client, today, rows):
    if not rows:
        return
    records = [{"run_date": today, **row} for row in rows]
    client.table("opportunities_snapshot").upsert(records, on_conflict="run_date,id").execute()


def diff_and_upsert_dim(client, today_iso, rows):
    """Compares today's fetch against opportunities_dim, applies changes, and
    returns (created_count, updated_count, closed_count, reopened_count)."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    today_by_id = {row["id"]: row for row in rows}

    existing = client.table("opportunities_dim").select("*").execute().data
    existing_by_id = {row["id"]: row for row in existing}

    dim_upserts = []
    events = []
    created = updated = closed = reopened = 0

    for opp_id, row in today_by_id.items():
        prior = existing_by_id.get(opp_id)

        if prior is None:
            new_record = {
                "id": opp_id,
                "first_seen_at": now,
                "last_seen_at": now,
                "is_active": True,
                "times_seen": 1,
            }
            for field in TRACKED_FIELDS:
                new_record[f"current_{field}"] = row[field]
            dim_upserts.append(new_record)
            events.append({"opportunity_id": opp_id, "event_type": "created"})
            created += 1
            continue

        record = {
            "id": opp_id,
            "first_seen_at": prior["first_seen_at"],
            "last_seen_at": now,
            "times_seen": prior["times_seen"] + 1,
            "is_active": True,
        }

        any_field_changed = False
        for field in TRACKED_FIELDS:
            old_value = prior.get(f"current_{field}")
            new_value = row[field]
            record[f"current_{field}"] = new_value
            if old_value != new_value:
                any_field_changed = True
                events.append({
                    "opportunity_id": opp_id,
                    "event_type": "updated",
                    "field": field,
                    "old_value": old_value,
                    "new_value": new_value,
                })

        if not prior["is_active"]:
            events.append({"opportunity_id": opp_id, "event_type": "reopened"})
            reopened += 1
        elif any_field_changed:
            updated += 1

        dim_upserts.append(record)

    for opp_id, prior in existing_by_id.items():
        if prior["is_active"] and opp_id not in today_by_id:
            closed_record = {
                "id": opp_id,
                "first_seen_at": prior["first_seen_at"],
                "last_seen_at": prior["last_seen_at"],
                "times_seen": prior["times_seen"],
                "is_active": False,
            }
            for field in TRACKED_FIELDS:
                closed_record[f"current_{field}"] = prior[f"current_{field}"]
            dim_upserts.append(closed_record)
            events.append({"opportunity_id": opp_id, "event_type": "closed"})
            closed += 1

    if dim_upserts:
        client.table("opportunities_dim").upsert(dim_upserts, on_conflict="id").execute()
    if events:
        client.table("opportunity_events").insert(events).execute()

    return created, updated, closed, reopened


def main():
    client = get_client()
    started = time.monotonic()
    run_id = start_run(client)

    try:
        today = datetime.date.today().isoformat()
        raw_pages, rows = fetch_all(earliest_start_date=today)

        archive_raw(client, today, raw_pages)
        upsert_snapshot(client, today, rows)
        created, updated, closed, reopened = diff_and_upsert_dim(client, today, rows)

        duration_ms = int((time.monotonic() - started) * 1000)
        finish_run(
            client,
            run_id,
            "success",
            pages_fetched=len(raw_pages),
            items_fetched=len(rows),
            duration_ms=duration_ms,
        )
        print(
            f"OK: {len(rows)} opportunities across {len(raw_pages)} pages "
            f"(+{created} created, {updated} updated, {closed} closed, {reopened} reopened)"
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        finish_run(client, run_id, "failed", duration_ms=duration_ms, error=str(exc)[:2000])
        raise


if __name__ == "__main__":
    sys.exit(main())
