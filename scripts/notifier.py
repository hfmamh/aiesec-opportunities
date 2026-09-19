"""Telegram notifications for newly created items across both pipelines
(AIESEC opportunities and convocatorias del Estado peruano).

Sends one grouped summary message per sync run per source (not one message
per item) via the Telegram Bot API, using only the stdlib (urllib) to match
the rest of this codebase's no-extra-deps style.
"""

import html
import json
import os
import time
import urllib.error
import urllib.request

TELEGRAM_API_BASE = "https://api.telegram.org"

# Telegram rejects messages over 4096 characters. _chunk_blocks/_send_grouped
# below split a run's items across multiple messages when needed, so a big
# backfill (or just a busy day) never silently fails to notify (see TODO.md).
TELEGRAM_MESSAGE_LIMIT = 4096

# Reserves room in each chunk's budget for its header line (e.g. "12 nuevas
# convocatorias del Estado (3/4)") plus the blank line before the first
# item. Generous vs. the ~60-80 chars a real header line runs.
HEADER_MARGIN = 200


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
    """Sends a message via the Telegram Bot API. Retries once on a 429
    (flood control), sleeping for the `retry_after` Telegram reports —
    needed because a chunked notification can send dozens of messages in a
    burst. Raises RuntimeError on any other failure (missing credentials,
    HTTP error, or an ok=false API response)."""
    token = _bot_token()
    chat_id = _chat_id()

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    for attempt in range(2):
        req = urllib.request.Request(
            f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt == 0:
                retry_after = 5
                try:
                    retry_after = json.loads(error_body)["parameters"]["retry_after"]
                except (json.JSONDecodeError, KeyError):
                    pass
                time.sleep(retry_after)
                continue
            raise RuntimeError(f"HTTP {e.code} sending Telegram message: {error_body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Failed to reach Telegram API: {e}") from e

    if not body.get("ok"):
        raise RuntimeError(f"Telegram API returned ok=false: {body}")
    return body


def _chunk_blocks(blocks, limit=TELEGRAM_MESSAGE_LIMIT, margin=HEADER_MARGIN):
    """Greedily groups pre-formatted item blocks (one per item) into chunks
    whose combined length stays within `limit` once `margin` is set aside
    for that chunk's header. Never splits a single item's block across two
    messages — if one block alone exceeds the budget, it still gets sent
    alone rather than dropped."""
    budget = limit - margin
    chunks = []
    current = []
    current_len = 0
    for block in blocks:
        block_len = len(block) + 2  # + the "\n\n" separator
        if current and current_len + block_len > budget:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(block)
        current_len += block_len
    if current:
        chunks.append(current)
    return chunks


def _send_grouped(header_fn, blocks):
    """Sends one Telegram message per chunk of `blocks`, chunked so none
    exceeds Telegram's character limit. header_fn(chunk_index, chunk_count)
    builds that chunk's header line (chunk_count == 1 for an unsplit run). A
    1s pace between messages keeps a large multi-chunk run under Telegram's
    per-chat flood-control limit (send_message also retries once on a 429,
    as a backstop if pacing alone isn't enough)."""
    chunks = _chunk_blocks(blocks)
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        header = header_fn(i, total)
        send_message(header + "\n\n" + "\n\n".join(chunk))
        if i < total:
            time.sleep(1)


# ============================================================
# AIESEC opportunities
# ============================================================

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


def _opportunities_header(count, chunk_index, chunk_count):
    noun = "nueva oportunidad" if count == 1 else "nuevas oportunidades"
    header = f"<b>{count} {noun} AIESEC</b>"
    if chunk_count > 1:
        header += f" ({chunk_index}/{chunk_count})"
    return header


def notify_new_opportunities(rows):
    """Sends one or more grouped summary messages (chunked to Telegram's
    character limit) for newly created AIESEC opportunities. No-ops if rows
    is empty. Raises on failure — it's up to the caller to decide whether a
    notification failure should affect the run's outcome."""
    if not rows:
        return
    count = len(rows)
    blocks = [_format_opportunity(row, i) for i, row in enumerate(rows, start=1)]
    _send_grouped(lambda i, total: _opportunities_header(count, i, total), blocks)


# ============================================================
# Convocatorias del Estado peruano
# ============================================================

def _format_convocatoria(row, index):
    titulo = html.escape(row.get("titulo") or "Convocatoria sin título")
    entidad = html.escape(row.get("entidad") or "Entidad desconocida")
    departamento = row.get("departamento")
    distrito = row.get("distrito")
    place_parts = [p for p in (departamento, distrito) if p]
    place = html.escape(" · ".join(place_parts)) if place_parts else ""

    lines = [f"{index}. <b>{titulo}</b>"]
    meta = f"<i>{entidad}</i>"
    if place:
        meta += f" — <i>{place}</i>"
    lines.append(meta)

    sueldo = row.get("sueldo")
    if sueldo:
        lines.append(f"\U0001f4b0 S/ {sueldo:,.0f}")

    details = []
    modalidad = row.get("modalidad")
    if modalidad:
        details.append(html.escape(str(modalidad)))
    vacantes = row.get("vacantes")
    if vacantes:
        details.append(f"{vacantes} vacante{'s' if vacantes != 1 else ''}")
    if details:
        lines.append(" · ".join(details))

    fecha_cierre = row.get("fecha_cierre")
    if fecha_cierre:
        lines.append(f"\U0001f4c5 Cierra {html.escape(str(fecha_cierre))}")

    url = row.get("url")
    if url:
        lines.append(f'<a href="{html.escape(url)}">Ver convocatoria →</a>')

    return "\n".join(lines)


def _convocatorias_header(count, chunk_index, chunk_count):
    noun = "nueva convocatoria" if count == 1 else "nuevas convocatorias"
    header = f"<b>{count} {noun} del Estado</b>"
    if chunk_count > 1:
        header += f" ({chunk_index}/{chunk_count})"
    return header


def notify_new_convocatorias(rows):
    """Sends one or more grouped summary messages (chunked to Telegram's
    character limit) for newly created convocatorias. Same no-op/raise
    contract as notify_new_opportunities."""
    if not rows:
        return
    count = len(rows)
    blocks = [_format_convocatoria(row, i) for i, row in enumerate(rows, start=1)]
    _send_grouped(lambda i, total: _convocatorias_header(count, i, total), blocks)
