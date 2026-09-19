"""One-off: sends a single Telegram notification for every convocatoria
currently matching the biology keyword filter, as a manual test of the new
keyword-scoped flow. Not part of the regular sync — the normal diff only
notifies genuinely new ('created') items, and these already existed in
convocatorias_dim from the pre-filter backfill, so they'd never trigger a
notification on their own. Uses today's convocatorias_snapshot rows (already
narrowed to the active keywords by today's filtered sync run). Deleted after
use — not part of the regular sync flow.
"""

import datetime

import notifier
from supabase_client import get_client


def main():
    client = get_client()
    today = datetime.date.today().isoformat()
    rows = (
        client.table("convocatorias_snapshot")
        .select("*")
        .eq("run_date", today)
        .execute()
        .data
    )
    print(f"Sending one-off notification for {len(rows)} biology convocatorias")
    notifier.notify_new_convocatorias(client, rows)
    print("Done")


if __name__ == "__main__":
    main()
