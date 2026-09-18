"""Telegram notifications for newly created AIESEC opportunities.

Sends one grouped summary message per sync run (not one message per
opportunity) via the Telegram Bot API, using only the stdlib (urllib) to
match the rest of this codebase's no-extra-deps style.
"""

import html
import json
import os
import urllib.error
import urllib.request

TELEGRAM_API_BASE = "https://api.telegram.org"


def _bot_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")
    return token


def _chat_id():
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID environment variable is not set")
    return chat_id


def send_message(text, parse_mode="HTML"):
    """Sends a message via the Telegram Bot API. Raises RuntimeError on any
    failure (missing credentials, HTTP error, or an ok=false API response)."""
    token = _bot_token()
    chat_id = _chat_id()

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    req = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} sending Telegram message: {error_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to reach Telegram API: {e}") from e

    if not body.get("ok"):
        raise RuntimeError(f"Telegram API returned ok=false: {body}")
    return body


def _format_opportunity(row, index):
    title = html.escape(row.get("title") or "Untitled opportunity")
    company = html.escape(row.get("company") or "Unknown company")
    country = row.get("country")
    place = html.escape(str(country)) if country else ""

    lines = [f"{index}. <b>{title}</b>"]
    meta = f"<i>{company}</i>"
    if place:
        meta += f" — <i>{place}</i>"
    lines.append(meta)

    salary = row.get("salary")
    if salary:
        salary_line = f"\U0001f4b0 {salary}"
        currency = row.get("salary_currency")
        if currency:
            salary_line += f" {html.escape(str(currency))}"
        periodicity = row.get("salary_periodicity")
        if periodicity:
            salary_line += f" / {html.escape(str(periodicity))}"
        lines.append(salary_line)

    opp_id = row.get("id")
    if opp_id:
        url = f"https://aiesec.org/opportunity/global-talent/{html.escape(str(opp_id))}"
        lines.append(f'<a href="{url}">Ver oportunidad →</a>')

    return "\n".join(lines)


def format_new_opportunities_message(rows):
    """Builds one grouped HTML summary message for the given newly-created
    opportunity rows (full dicts with title/location/country/company/salary/
    salary_currency/salary_periodicity, as produced by aiesec_client.to_rows)."""
    count = len(rows)
    noun = "nueva oportunidad" if count == 1 else "nuevas oportunidades"
    header = f"<b>{count} {noun} AIESEC</b>"
    blocks = [_format_opportunity(row, i) for i, row in enumerate(rows, start=1)]
    return header + "\n\n" + "\n\n".join(blocks)


def notify_new_opportunities(rows):
    """Sends one grouped summary message for newly created opportunities.
    No-ops if rows is empty. Raises on failure — it's up to the caller to
    decide whether a notification failure should affect the run's outcome."""
    if not rows:
        return
    message = format_new_opportunities_message(rows)
    send_message(message)
