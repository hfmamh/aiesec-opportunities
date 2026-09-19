"""One-off: sends a test message to every configured notification channel,
to confirm each source's chat_id actually routes to the right Telegram
group. Deleted after use — not part of the regular sync flow.
"""

import notifier
from supabase_client import get_client


def main():
    client = get_client()
    channels = client.table("notification_channels").select("*").execute().data
    for row in channels:
        source = row["source"]
        chat_id = row["chat_id"]
        print(f"Sending test message to source={source} chat_id={chat_id}")
        notifier.send_message(chat_id, f"<b>Test</b>: this chat is now wired to source '{source}'.")
    print("Done")


if __name__ == "__main__":
    main()
