"""Medición de cobertura: libro de visitas vs inventario independiente.

La pregunta que responde: de todo lo que un sitio TIENE, ¿cuánto vio el
pipeline, cuánto descargó y cuánto guardó? Y de lo que faltó, ¿por qué?

Dos entradas:
  - inventario: conjunto de URLs del sitio obtenido por una vía ajena al
    pipeline (sitemap.xml, rastreo de referencia con wget, API de la
    institución, o una lista hecha a mano). Es la "verdad" contra la que
    se mide; su calidad acota la del resultado.
  - libro de visitas (`storage/ledger.py`): toda URL que el pipeline vio
    y la decisión tomada.

Embudo por URL del inventario:

    inventario ──► vista (enqueued/rejected) ──► descargada ──► guardada
         │              │                          │
         │              └─ no vista: el crawl       └─ descartada: listing,
         │                 nunca llegó (hueco de       hub, no_document...
         │                 NAVEGACIÓN)                 (decisión de EXTRACCIÓN,
         └─ utilitaria: se excluye del                 auditable una a una)
            denominador (login, buscador,
            feeds, tags, estáticos)

Métricas (sobre URLs de contenido del inventario, C):
    alcance      |C ∩ vistas|     / |C|   ¿la navegación llega?
    descarga     |C ∩ fetched|    / |C|   ¿el presupuesto alcanzó?
    recall       |C ∩ guardadas|  / |C|   ¿terminó en el corpus?
    precisión    |guardadas ∩ inventario| / |guardadas|
                 (lo guardado fuera del inventario no es error: suele ser
                 lo que el inventario no vio; se reporta como "extra")
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

FETCH_OUTCOMES = frozenset({
    "saved", "duplicate", "listing", "hub", "no_document", "blocked", "robots",
    "http_error", "throttle",
    "already_saved", "too_large", "empty", "offsite_html", "extract_error"})
SEEN = FETCH_OUTCOMES | {"enqueued", "rejected", "not_fetched"}

# URLs que no son contenido en casi ningún CMS: no cuentan en el denominador.
UTILITY_PAT = re.compile(
    r"/(wp-login\.php|wp-admin|wp-json|xmlrpc\.php|login|logout|signin|register|cuenta|"
    r"account|cart|carrito|checkout|search|buscar|busqueda|feed|rss|comments/feed|"
    r"tag|tags|etiqueta|author|autor|category|categoria|page/\d+|wp-content/uploads/"
    r"[^/]+\.(jpe?g|png|gif|svg|webp))(/|$|\?)"
    r"|[?&](s|q|query|search|replytocom|share|print|utm_\w+|fbclid)="
    r"|[?&]page=\d+"        # paginación de listados: navegación, no documento
    r"|\.(css|js|jpe?g|png|gif|svg|webp|ico|woff2?|ttf|eot|mp[34]|zip|xml|json)(\?|$)"
    r"|/#|/sitemap", re.I)

DOC_EXT = (".pdf", ".docx", ".doc", ".odt", ".rtf")

# Formatos que el pipeline NO extrae: planillas y datos tabulares/geográficos.
# Cuentan como contenido del sitio, pero medir recall contra ellos castiga al
# pipeline por algo que no intenta hacer. Un portal de gobierno con datos
# abiertos los tiene por cientos: sernatur.cl trae 121 entre .xlsx, .xls,
# .csv y .kmz sobre 3.200 URLs de contenido.
NON_EXTRACTABLE_EXT = (".xlsx", ".xls", ".csv", ".tsv", ".kmz", ".kml",
                       ".zip", ".rar", ".7z", ".ppt", ".pptx", ".dwg",
                       ".shp", ".geojson", ".json", ".xml", ".mdb", ".accdb")

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
             "utm_content", "fbclid", "gclid"}


def normalize(url: str) -> str:
    """Forma canónica para cruzar conjuntos: sin fragmento, sin tracking,
    esquema/host en minúsculas, sin barra final (salvo raíz)."""
    u = urlparse(url.strip())
    scheme = (u.scheme or "https").lower()
    host = u.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", u.path) or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    if path.endswith(("/index.html", "/index.php", "/index.htm")):
        path = path.rsplit("/", 1)[0] or "/"
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
         if k not in _TRACKING]
    return urlunparse((scheme, host, path, "", urlencode(sorted(q)), ""))


def host_of(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def is_utility(url: str) -> bool:
    return bool(UTILITY_PAT.search(url))


def is_document(url: str) -> bool:
    return url.lower().split("?")[0].endswith(DOC_EXT)


def is_extractable(url: str) -> bool:
    """¿El pipeline tiene un extractor para esto? HTML, PDF y Word sí;
    planillas, datos tabulares y archivos comprimidos no."""
    return not url.lower().split("?")[0].endswith(NON_EXTRACTABLE_EXT)


# ----------------------------------------------------------------------
def ledger_by_url(rows: list[dict], host: str | None = None,
                  index_urls: set[str] | None = None) -> dict[str, dict]:
    """Colapsa el libro por URL normalizada: última decisión de fetch (si la
    hubo), si fue vista, y cómo se descubrió.

    `index_urls` (URLs realmente presentes en metadata/index.json) corrige
    libros anteriores a la decisión `duplicate`: un `saved` cuya URL no está
    en el índice fue un alias por contenido, no un documento."""
    idx_norm = {normalize(u) for u in index_urls} if index_urls is not None else None
    out: dict[str, dict] = {}
    for r in rows:
        url = r.get("url")
        if not url:
            continue
        if host and host_of(url) != host:
            continue
        key = normalize(url)
        e = out.setdefault(key, {"url": url, "seen": False, "fetch": None,
                                 "reason": None, "kind": None, "via": None,
                                 "not_fetched": None, "rejected": None})
        d = r.get("decision")
        if d == "saved" and idx_norm is not None and key not in idx_norm:
            d = "duplicate"
        e["seen"] = True
        if d == "enqueued":
            e["kind"] = e["kind"] or r.get("kind")
            e["via"] = e["via"] or r.get("via")
        elif d == "rejected":
            e["rejected"] = r.get("reason")
        elif d == "not_fetched":
            e["not_fetched"] = r.get("reason")
        elif d in FETCH_OUTCOMES:
            # saved gana a cualquier otro resultado (reintentos, aliases)
            if e["fetch"] != "saved":
                e["fetch"], e["reason"] = d, r.get("reason")
    return out


def funnel(by_url: dict[str, dict]) -> dict:
    """Embudo del propio crawl, sin inventario: qué pasó con lo que vio."""
    c = Counter()
    for e in by_url.values():
        if e["fetch"]:
            c[e["fetch"]] += 1
        elif e["not_fetched"]:
            c["not_fetched"] += 1
        elif e["rejected"]:
            c[f"rejected:{e['rejected']}"] += 1
        else:
            c["enqueued_only"] += 1
    return {"urls_seen": len(by_url), "fetched": sum(
        v for k, v in c.items() if k in FETCH_OUTCOMES),
        "saved": c.get("saved", 0), "by_decision": dict(c.most_common())}


def compare(inventory: dict[str, str], by_url: dict[str, dict],
            extra_utility: re.Pattern | None = None) -> dict:
    """inventory: {url_normalizada: fuente}. Devuelve métricas + faltantes."""
    content, utility, no_extraible = {}, {}, {}
    for key, src in inventory.items():
        if is_utility(key) or (extra_utility and extra_utility.search(key)):
            utility[key] = src
        elif not is_extractable(key):
            no_extraible[key] = src
        else:
            content[key] = src

    seen = {k for k in content if k in by_url and by_url[k]["seen"]}
    fetched = {k for k in seen if by_url[k]["fetch"]}
    saved = {k for k in fetched if by_url[k]["fetch"] == "saved"}

    missing = []
    for k in sorted(content):
        if k in saved:
            continue
        e = by_url.get(k)
        if e is None or not e["seen"]:
            stage, why = "no_vista", "el crawl nunca llegó a esta URL"
        elif e["fetch"]:
            stage, why = "descartada", f"{e['fetch']}" + (
                f" ({e['reason']})" if e["reason"] else "")
        elif e["not_fetched"]:
            stage, why = "sin_descargar", e["not_fetched"]
        elif e["rejected"]:
            stage, why = "rechazada", e["rejected"]
        else:
            stage, why = "sin_descargar", "quedó encolada"
        missing.append({"url": k, "source": content[k], "stage": stage,
                        "why": why, "document": is_document(k)})

    all_saved = {k for k, e in by_url.items() if e["fetch"] == "saved"}
    in_inventory = {k for k in all_saved if k in inventory}
    extra = sorted(all_saved - set(inventory))

    n = len(content)
    pct = lambda x: round(100 * x / n, 1) if n else None
    return {
        "inventory_total": len(inventory),
        "inventory_content": n,
        "inventory_utility": len(utility),
        "inventory_no_extraible": len(no_extraible),
        "seen": len(seen), "fetched": len(fetched), "saved": len(saved),
        "reach_pct": pct(len(seen)), "fetch_pct": pct(len(fetched)),
        "recall_pct": pct(len(saved)),
        "precision_pct": (round(100 * len(in_inventory) / len(all_saved), 1)
                          if all_saved else None),
        "saved_total": len(all_saved),
        "saved_outside_inventory": len(extra),
        "missing_by_stage": dict(Counter(m["stage"] for m in missing)),
        "missing_by_why": dict(Counter(
            m["why"].split(" (")[0] for m in missing).most_common(12)),
        "missing": missing,
        "extra": extra,
    }


# ----------------------------------------------------------------------
# Inventario desde un log de `wget --spider -r --no-verbose`
# ----------------------------------------------------------------------
# wget escribe DOS formas de línea según cómo trató la URL:
#   ts URL:<u> 200 OK                         hoja comprobada con HEAD (PDF, img)
#   ts URL:<u> [12345/12345] -> "ruta" [1]    HTML descargado para seguir enlaces
#                                             (y borrado: --spider)
# Solo la primera trae código HTTP; la segunda implica 200. Leer ambas o
# el inventario pierde todas las páginas HTML (error real de la v1).
# El corchete del tamaño viene en DOS formas: "[31844/31844]" cuando wget
# conoce el content-length, y "[35872]" cuando no. Aceptar solo la primera
# hacía perder todas las páginas HTML de los sitios que no lo declaran: el
# inventario de un sitio cayó de 328 páginas a 12.
_WGET_LINE = re.compile(r"URL:\s*(\S+)\s+(?:(\d{3})\b|\[\d+(?:/\d+)?\])")


def parse_wget_log(text: str, host: str) -> list[str]:
    urls: list[str] = []
    for line in text.splitlines():
        m = _WGET_LINE.search(line)
        if not m:
            continue
        url, status = m.group(1), m.group(2)
        if status and not status.startswith("2"):
            continue
        if host_of(url) == host:
            urls.append(url)
    return list(dict.fromkeys(urls))
