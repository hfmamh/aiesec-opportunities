import datetime
import json
import os
import urllib.error
import urllib.request

GRAPHQL_URL = "https://gis-api.aiesec.org/graphql"

QUERY = """
query SearchOpportunityTitles($page: Int, $per_page: Int, $filters: OpportunityFilter) {
  allOpportunity: opportunities(
    filters: $filters
    pagination: { page: $page, per_page: $per_page }
  ) {
    data {
      id
      location
      title
      host_lc {
        address_detail {
          country
        }
      }
      branch {
        company {
          name
        }
      }
      specifics_info {
        salary
        salary_currency {
          alphabetic_code
        }
        salary_periodicity
      }
    }
    paging {
      total_items
      total_pages
      current_page
    }
  }
}
"""


def _dig(obj, *keys):
    """Safely walks nested dicts, returning None as soon as a key is missing."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


# Single source of truth for what gets tracked per opportunity. To track a new
# field: add it to QUERY above, then add one entry here — sync.py, the
# snapshot table, and the diff/event logic all pick it up automatically.
# You'll still need to add the matching column(s) in sql/schema.sql (see
# sql/migrations/ for how to add one to the live database).
FIELD_EXTRACTORS = {
    "title": lambda op: op.get("title"),
    "location": lambda op: op.get("location"),
    "country": lambda op: _dig(op, "host_lc", "address_detail", "country"),
    "company": lambda op: _dig(op, "branch", "company", "name"),
    "salary": lambda op: _dig(op, "specifics_info", "salary"),
    "salary_currency": lambda op: _dig(op, "specifics_info", "salary_currency", "alphabetic_code"),
    "salary_periodicity": lambda op: _dig(op, "specifics_info", "salary_periodicity"),
}


def _api_key():
    key = os.environ.get("AIESEC_API_KEY")
    if not key:
        raise RuntimeError("AIESEC_API_KEY environment variable is not set")
    return key


def fetch_page(page=1, per_page=200, programmes=(8,), earliest_start_date=None):
    if earliest_start_date is None:
        earliest_start_date = datetime.date.today().isoformat()

    payload = {
        "operationName": "SearchOpportunityTitles",
        "variables": {
            "page": page,
            "per_page": per_page,
            "filters": {
                "q": None,
                "sort": "created_at",
                "sort_direction": "desc",
                "programmes": list(programmes),
                "earliest_start_date": {"from": earliest_start_date},
            },
        },
        "query": QUERY,
    }

    req = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://aiesec.org",
            "Referer": "https://aiesec.org/",
            "authorization": _api_key(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} fetching page {page}: {body}") from e


def fetch_all(per_page=200, programmes=(8,), earliest_start_date=None):
    """Fetches every page of opportunities. Returns (raw_pages, flattened_rows)."""
    raw_pages = []
    rows = []

    page = 1
    total_pages = 1
    while page <= total_pages:
        result = fetch_page(
            page=page,
            per_page=per_page,
            programmes=programmes,
            earliest_start_date=earliest_start_date,
        )
        raw_pages.append(result)

        block = result["data"]["allOpportunity"]
        total_pages = block["paging"]["total_pages"]
        rows.extend(to_rows(result))
        page += 1

    return raw_pages, rows


def to_rows(result):
    """Flattens a single page's GraphQL response into simple dicts, one per
    field in FIELD_EXTRACTORS plus id."""
    rows = []
    items = result["data"]["allOpportunity"]["data"]
    for op in items:
        row = {"id": str(op.get("id"))}
        for field, extract in FIELD_EXTRACTORS.items():
            row[field] = extract(op)
        rows.append(row)
    return rows
