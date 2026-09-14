import json
import urllib.request
import urllib.error
import csv

GRAPHQL_URL = "https://gis-api.aiesec.org/graphql"

# Clave publica fija embebida en el frontend de aiesec.org para consultas
# anonimas al GraphQL. Confirmado empiricamente: no cambia entre recargas
# de pagina ni depende de cookies/sesion (a diferencia de un token normal
# que expira). Si en el futuro deja de funcionar (402/403), hay que volver
# a capturarla desde DevTools -> Network -> request a gis-api.aiesec.org ->
# pestana Headers -> Request Headers -> "authorization".
PUBLIC_API_KEY = "e316ebe109dd84ed16734e5161a2d236d0a7e6daf499941f7c110078e3c75493"

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
    }
    paging {
      total_items
      total_pages
      current_page
    }
  }
}
"""


def fetch_opportunities(page=1, per_page=20, programmes=(8,), earliest_start_date="2026-09-11"):
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
            "authorization": PUBLIC_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP Error {e.code}: {e.reason}")
        print("Body de la respuesta de error:")
        print(body)
        raise


def to_rows(result):
    """Aplana la respuesta GraphQL a filas simples: id, title, location, country, company."""
    rows = []
    items = result["data"]["allOpportunity"]["data"]
    for op in items:
        country = None
        if op.get("host_lc") and op["host_lc"].get("address_detail"):
            country = op["host_lc"]["address_detail"].get("country")

        company = None
        if op.get("branch") and op["branch"].get("company"):
            company = op["branch"]["company"].get("name")

        rows.append({
            "id": op.get("id"),
            "title": op.get("title"),
            "location": op.get("location"),
            "country": country,
            "company": company,
        })
    return rows


def print_table(rows):
    """Imprime una tabla simple en consola sin dependencias externas."""
    if not rows:
        print("Sin resultados.")
        return

    headers = ["id", "title", "location", "country", "company"]
    widths = {h: max(len(h), max(len(str(r[h]) if r[h] is not None else "") for r in rows)) for h in headers}

    def fmt_row(values):
        return " | ".join(str(v).ljust(widths[h]) for h, v in zip(headers, values))

    print(fmt_row(headers))
    print("-+-".join("-" * widths[h] for h in headers))
    for r in rows:
        print(fmt_row([r[h] if r[h] is not None else "" for h in headers]))


def save_csv(rows, path="aiesec_opportunities.csv"):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "title", "location", "country", "company"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nGuardado en: {path}")


if __name__ == "__main__":
    result = fetch_opportunities(page=1, per_page=500)
    paging = result["data"]["allOpportunity"]["paging"]
    rows = to_rows(result)

    print(f"Pagina {paging['current_page']} de {paging['total_pages']} (total: {paging['total_items']} oportunidades)\n")
    print_table(rows)
    save_csv(rows)