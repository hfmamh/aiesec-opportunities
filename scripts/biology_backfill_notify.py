"""One-off: sends a single Telegram notification for every convocatoria
currently matching the biology keyword filter, as a manual test of the new
keyword-scoped flow. Not part of the regular sync — the normal diff only
notifies genuinely new ('created') items, and these already existed in
convocatorias_dim from the pre-filter backfill, so they'd never trigger a
notification on their own.

Reads convocatorias_dim where is_active=true directly (not a snapshot/date
filter) so it reflects exactly what the filter currently matches, regardless
of how many sync runs happened today. A previous version of this script
filtered convocatorias_snapshot by today's run_date instead, which
accidentally included two earlier *unfiltered* runs from before the filter
was deployed and notified nearly the full catalog — SAFETY_CAP below guards
against that class of mistake recurring.

Deleted after use — not part of the regular sync flow.
"""

import convocatorias_client
import notifier
from supabase_client import get_client

SAFETY_CAP = 200
CONVOCATORIAS_FIELDS = list(convocatorias_client.FIELD_EXTRACTORS.keys())


def main():
    client = get_client()
    dim_rows = (
        client.table("convocatorias_dim")
        .select("*")
        .eq("is_active", True)
        .execute()
        .data
    )

    if len(dim_rows) > SAFETY_CAP:
        raise RuntimeError(
            f"{len(dim_rows)} active convocatorias, over the {SAFETY_CAP} "
            "safety cap for a one-off manual send — aborting without "
            "sending anything. Check convocatoria_keywords before rerunning."
        )

    rows = [
        {field: row.get(f"current_{field}") for field in CONVOCATORIAS_FIELDS}
        | {"id": row["id"]}
        for row in dim_rows
    ]

    print(f"Sending one-off notification for {len(rows)} biology convocatorias")
    notifier.notify_new_convocatorias(client, rows)
    print("Done")


if __name__ == "__main__":
    main()
