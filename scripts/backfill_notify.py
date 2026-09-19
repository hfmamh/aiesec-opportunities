"""One-off: sends the Telegram notification for the convocatorias backfill
that ran before chunking was fixed (its 413 meant nobody got notified, even
though the data itself is safely in Supabase). Deleted after use — not part
of the regular sync flow.
"""

import notifier
from supabase_client import get_client


def main():
    client = get_client()
    rows = client.table("convocatorias_snapshot").select("*").execute().data
    print(f"Sending backfill notification for {len(rows)} convocatorias")
    notifier.notify_new_convocatorias(rows)
    print("Done")


if __name__ == "__main__":
    main()
