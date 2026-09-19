"""Scrapes convocatoriasestado.pe's public job-listing pages.

There's no official API tier we can afford (see README), so this pages
through the plain server-rendered HTML at /convocatorias/?page=N. The site's
robots.txt explicitly allows this path (and even lists AI crawlers by name),
and the page has no anti-bot layer, so a polite delay between requests is
the only courtesy this needs.

Follows the same shape as aiesec_client.py: FIELD_EXTRACTORS is the single
source of truth for what gets tracked per listing. To track a new field:
1. Make sure _parse_article captures the raw string for it.
2. Add one entry to FIELD_EXTRACTORS mapping a column name to a function
   that turns that raw string into a typed value.
You'll still need to add the matching column(s) in sql/schema.sql (see
sql/migrations/ for how to add one to the live database).
"""

import datetime
import html
import re
import time
import urllib.error
import urllib.request

BASE_URL = "https://convocatoriasestado.pe"
LISTING_PATH = "/convocatorias/"

# Between page requests. 191 pages at 0.5s apart is ~95s per run, well within
# a GitHub Actions job's budget, and keeps the request rate polite (~2/s).
REQUEST_DELAY_SECONDS = 0.5
MAX_PAGES = 500  # safety cap; real loop stops as soon as a page has 0 cards

ARTICLE_RE = re.compile(r'<article class="card">(.*?)</article>', re.DOTALL)
LINK_RE = re.compile(r'<a href="(/convocatoria/(\d+)-[^"]*?/)"[^>]*>([^<]+)</a>', re.DOTALL)
ENTIDAD_RE = re.compile(r'<p class="card-entidad">([^<]*)</p>')
SUELDO_RE = re.compile(r'<span class="sueldo">([^<]*)</span>')
UBICACION_RE = re.compile(r'#i-pin"></use></svg>([^<]*)</span>')
VACANTES_RE = re.compile(r'#i-vacantes"></use></svg>([^<]*)</span>')
MODALIDAD_RE = re.compile(r'#i-maletin"></use></svg>([^<]*)</span>')
BASES_RE = re.compile(r'class="card-docs[^"]*">.*?</svg>([^<]*)</span>', re.DOTALL)
CIERRE_RE = re.compile(r'class="cierre[^"]*">.*?</svg>([^<]*)</span>', re.DOTALL)

CIERRE_FECHA_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
CIERRE_DIAS_RE = re.compile(r"en (\d+)\s*d[ií]a")


def _text(match):
    return html.unescape(match.group(1)).strip() if match else None


def _parse_int(raw):
    if not raw:
        return None
    m = re.search(r"\d+", raw)
    return int(m.group(0)) if m else None


def _parse_sueldo(raw):
    if not raw:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", raw)
    if not m:
        return None
    return float(m.group(0).replace(",", ""))


def _split_ubicacion(raw):
    if not raw:
        return None, None
    parts = [p.strip() for p in raw.split("·")]  # '·'
    if len(parts) == 2:
        return parts[0], parts[1]
    return raw.strip(), None


def _parse_cierre(raw, today=None):
    """Best-effort parse of the closing-date text into an ISO date. Returns
    None for phrasing this doesn't recognize rather than guessing."""
    if not raw:
        return None
    if today is None:
        today = datetime.date.today()

    m = CIERRE_FECHA_RE.search(raw)
    if m:
        day, month, year = (int(g) for g in m.groups())
        return datetime.date(year, month, day).isoformat()

    lowered = raw.lower()
    if "hoy" in lowered:
        return today.isoformat()
    if "mañana" in lowered:
        return (today + datetime.timedelta(days=1)).isoformat()

    m = CIERRE_DIAS_RE.search(lowered)
    if m:
        return (today + datetime.timedelta(days=int(m.group(1)))).isoformat()

    return None


def _parse_article(chunk):
    """Extracts the raw strings for one <article class="card"> block. Returns
    None if the block doesn't have the one field we can't do without (id)."""
    link = LINK_RE.search(chunk)
    if not link:
        return None

    return {
        "id": link.group(2),
        "url_path": link.group(1),
        "titulo_raw": link.group(3),
        "entidad_raw": _text(ENTIDAD_RE.search(chunk)),
        "sueldo_raw": _text(SUELDO_RE.search(chunk)),
        "ubicacion_raw": _text(UBICACION_RE.search(chunk)),
        "vacantes_raw": _text(VACANTES_RE.search(chunk)),
        "modalidad_raw": _text(MODALIDAD_RE.search(chunk)),
        "bases_raw": _text(BASES_RE.search(chunk)),
        "cierre_raw": _text(CIERRE_RE.search(chunk)),
    }


FIELD_EXTRACTORS = {
    "titulo": lambda r: html.unescape(r["titulo_raw"]).strip() if r.get("titulo_raw") else None,
    "entidad": lambda r: r.get("entidad_raw"),
    "departamento": lambda r: _split_ubicacion(r.get("ubicacion_raw"))[0],
    "distrito": lambda r: _split_ubicacion(r.get("ubicacion_raw"))[1],
    "vacantes": lambda r: _parse_int(r.get("vacantes_raw")),
    "sueldo": lambda r: _parse_sueldo(r.get("sueldo_raw")),
    "modalidad": lambda r: r.get("modalidad_raw"),
    "tiene_bases": lambda r: bool(r.get("bases_raw")) and not r["bases_raw"].lower().startswith("sin"),
    "fecha_cierre": lambda r: _parse_cierre(r.get("cierre_raw")),
    "url": lambda r: f"{BASE_URL}{r['url_path']}" if r.get("url_path") else None,
}


def fetch_page(page=1):
    url = f"{BASE_URL}{LISTING_PATH}?page={page}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; convocatorias-aiesec-pipeline/1.0; "
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
        if e.code == 404:
            # The site 404s once you page past the last one, instead of
            # returning a 200 with zero cards — that's how fetch_all knows
            # pagination is over.
            return None
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} fetching page {page}: {body[:500]}") from e


def to_rows(raw_items):
    """Flattens a page's list of raw parsed dicts into row dicts, one per
    field in FIELD_EXTRACTORS plus id, matching aiesec_client.to_rows."""
    rows = []
    for raw in raw_items:
        row = {"id": raw["id"]}
        for field, extract in FIELD_EXTRACTORS.items():
            row[field] = extract(raw)
        rows.append(row)
    return rows


def fetch_all():
    """Pages through the full active listing. Returns (raw_pages, rows) where
    raw_pages is a list of {"page": n, "items": [raw dicts]} (used for the
    Storage archive) and rows is every listing flattened via
    FIELD_EXTRACTORS."""
    raw_pages = []
    rows = []

    page = 1
    while page <= MAX_PAGES:
        html_text = fetch_page(page)
        if html_text is None:
            break
        articles = [
            parsed
            for chunk in ARTICLE_RE.findall(html_text)
            if (parsed := _parse_article(chunk)) is not None
        ]
        if not articles:
            break

        raw_pages.append({"page": page, "items": articles})
        rows.extend(to_rows(articles))

        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    return raw_pages, rows
