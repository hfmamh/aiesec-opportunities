"""Scrapes Get on Board's public job-listing pages (getonbrd.com.pe).

Unlike convocatorias_client.py, this does NOT fetch each job's detail page.
The tag listing page itself already renders title, company, location,
employment type, salary (when the poster filled it in) and a posted date for
every card — fetching ~30 detail pages per run on top of that would roughly
30x the request volume and the raw-snapshots archive size for fields we
don't need yet. If a future field genuinely requires the detail page (e.g.
the full description), add a second fetch keyed off the ids that are new
this run, not every id every run.

robots.txt on getonbrd.com.pe only disallows a named list of AI-training
crawlers (GPTBot, ClaudeBot, Bytespider, etc.); the generic `User-agent: *`
block is `Allow: /`, and this client's User-Agent string doesn't match any
of the disallowed names. No anti-bot/JS-challenge layer was observed on a
plain fetch (server is Cloudflare, but doesn't appear to gate this path).

Only one search tag is wired up for now ("data-engineer"), not a
configurable keywords table like convocatorias_keywords (see sync.py) —
revisit that if/when more tags are needed.

Follows the same shape as aiesec_client.py / convocatorias_client.py:
FIELD_EXTRACTORS is the single source of truth for what gets tracked per
listing. To track a new field:
1. Make sure _parse_card captures the raw string for it.
2. Add one entry to FIELD_EXTRACTORS mapping a column name to a function
   that turns that raw string into a typed value.
You'll still need to add the matching column(s) via a new file in
sql/migrations/ (and mirror them into sql/schema.sql).

Natural id: there's no numeric job id anywhere in the listing HTML (unlike
AIESEC's opportunity id or convocatorias' numeric id in the URL). The only
stable identifier is the job's URL path (e.g.
"/jobs/data-science-analytics/data-engineer-codeable-lima"), so that's what
this uses as `id`. The scheme+host is stripped before using it as the id:
one card in the wild was observed with an inexplicable `:2053` port on its
href (likely a Cloudflare edge quirk) while every other card and every
re-fetch of that same job had no port at all — keying on the path only
avoids that turning into spurious duplicate/closed/reopened churn.
"""

import datetime
import html
import re
import urllib.error
import urllib.parse
import urllib.request

LISTING_HOST = "https://www.getonbrd.com.pe"
JOB_HOST = "https://www.getonbrd.com"  # host the site itself emits in card hrefs
TAG = "data-engineer"

CARD_START_RE = re.compile(r'<a class="results-item')
HREF_RE = re.compile(r'href="([^"]+)"')
TITLE_RE = re.compile(r'<strong class="pr-3">(.*?)</strong>', re.DOTALL)
TIPO_EMPLEO_RE = re.compile(
    r'<strong class="pr-3">.*?</strong>\s*<span class="opacity-half">([^<]+)</span>', re.DOTALL
)
EMPRESA_RE = re.compile(r"<strong>([^<]+)</strong>")
UBICACION_RE = re.compile(
    r'<span class="location">(.*?)(?:<div class="location-tooltip-content|</span>\s*</span>\s*</span>)',
    re.DOTALL,
)
SALARIO_RE = re.compile(r"icon-money-bill.*?<span>([^<]+)</span>", re.DOTALL)
FECHA_RE = re.compile(r'<div class="opacity-half size0">\s*([^<]+?)\s*</div>')
TAG_STRIP_RE = re.compile(r"<[^>]+>")

# The site renders posted dates as "<Mon> <DD>" with no year, and mixes
# Spanish and English month abbreviations inconsistently (observed "Oct 09",
# "oct 09", "ago 18", "Jul 07" all on the same page). Covers both.
MONTHS = {
    "ene": 1, "jan": 1,
    "feb": 2,
    "mar": 3,
    "abr": 4, "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8, "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dic": 12, "dec": 12,
}
FECHA_TEXT_RE = re.compile(r"^([A-Za-z]{3})\s+0?(\d{1,2})$")


def _clean(raw_fragment):
    """Strips HTML tags out of a fragment (card text often has a nested
    <i> icon or tooltip <span> in it), unescapes entities, and collapses
    whitespace. Returns None for an empty/missing fragment."""
    if not raw_fragment:
        return None
    text = TAG_STRIP_RE.sub(" ", raw_fragment)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _parse_fecha(raw, today=None):
    """Best-effort parse of the card's "<Mon> <DD>" posted-date text into an
    ISO date, inferring the year (today's year, or last year if that would
    put the date in the future). Returns None for phrasing this doesn't
    recognize rather than guessing."""
    if not raw:
        return None
    if today is None:
        today = datetime.date.today()

    m = FECHA_TEXT_RE.match(raw.strip())
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    if month is None:
        return None
    day = int(m.group(2))

    try:
        date = datetime.date(today.year, month, day)
    except ValueError:
        return None
    if date > today:
        try:
            date = date.replace(year=today.year - 1)
        except ValueError:
            return None
    return date.isoformat()


def _parse_card(chunk):
    """Extracts the raw strings for one job card (an entire <a
    class="results-item" ...>...</a> block). Returns None if the block is
    missing the one thing we can't do without (href)."""
    href = HREF_RE.search(chunk)
    if not href:
        return None

    path = urllib.parse.urlparse(href.group(1)).path
    segments = [s for s in path.split("/") if s]  # e.g. ["jobs", "data-science-analytics", "slug"]
    categoria = segments[1] if len(segments) > 1 else None

    titulo = TITLE_RE.search(chunk)
    tipo_empleo = TIPO_EMPLEO_RE.search(chunk)
    empresa = EMPRESA_RE.search(chunk)
    ubicacion = UBICACION_RE.search(chunk)
    salario = SALARIO_RE.search(chunk)
    fecha = FECHA_RE.search(chunk)

    return {
        "id": path,
        "titulo_raw": _clean(titulo.group(1)) if titulo else None,
        "empresa_raw": _clean(empresa.group(1)) if empresa else None,
        "ubicacion_raw": _clean(ubicacion.group(1)) if ubicacion else None,
        "tipo_empleo_raw": _clean(tipo_empleo.group(1)) if tipo_empleo else None,
        "categoria_raw": categoria,
        "salario_raw": _clean(salario.group(1)) if salario else None,
        "fecha_raw": _clean(fecha.group(1)) if fecha else None,
    }


FIELD_EXTRACTORS = {
    "titulo": lambda r: r.get("titulo_raw"),
    "empresa": lambda r: r.get("empresa_raw"),
    "ubicacion": lambda r: r.get("ubicacion_raw"),
    "tipo_empleo": lambda r: r.get("tipo_empleo_raw"),
    "categoria": lambda r: r.get("categoria_raw"),
    "salario": lambda r: r.get("salario_raw"),
    "fecha_publicacion": lambda r: _parse_fecha(r.get("fecha_raw")),
    "url": lambda r: f"{JOB_HOST}{r['id']}" if r.get("id") else None,
}


def fetch_listing(tag=TAG):
    """Fetches the single listing page for `tag`. Returns the raw HTML, or
    raises RuntimeError on a non-2xx response.

    No pagination: as of 2026-10, this tag's listing returns ~30 results on
    one page with no detected real pagination (a `?page=2` request returned
    byte-for-byte the same set of cards as page 1 — the site appears to
    ignore that param for this listing style). If a tag ever grows past
    whatever this page caps at, results would start silently going missing;
    investigate the real pagination mechanism (it may not be `?page=N`)
    before assuming a bigger per-page limit or real pagination is safe to
    add.
    """
    url = f"{LISTING_HOST}/jobs/tag/{urllib.parse.quote(tag)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; getonboard-aiesec-pipeline/1.0; "
                "+https://github.com/) personal-project-sync"
            ),
            "Accept": "text/html",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} fetching tag {tag!r}: {body[:500]}") from e


def to_rows(raw_items):
    """Flattens a list of raw parsed card dicts into row dicts, one per
    field in FIELD_EXTRACTORS plus id, matching the other clients' to_rows."""
    rows = []
    for raw in raw_items:
        row = {"id": raw["id"]}
        for field, extract in FIELD_EXTRACTORS.items():
            row[field] = extract(raw)
        rows.append(row)
    return rows


def fetch_all(tag=TAG):
    """Fetches and parses the listing for `tag`. Returns (raw_pages, rows)
    matching aiesec_client/convocatorias_client's shape: raw_pages is a
    one-element list (used for the Storage archive, kept as a list for
    consistency with the paginated sources) and rows is every card
    flattened via FIELD_EXTRACTORS."""
    html_text = fetch_listing(tag=tag)
    cards = []
    starts = [m.start() for m in CARD_START_RE.finditer(html_text)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(html_text)
        chunk = html_text[start:end]
        close = chunk.find("</a>")
        if close != -1:
            chunk = chunk[:close]
        parsed = _parse_card(chunk)
        if parsed is not None:
            cards.append(parsed)

    raw_pages = [{"tag": tag, "items": cards}]
    rows = to_rows(cards)
    return raw_pages, rows
