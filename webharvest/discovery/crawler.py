"""Descubrimiento de "áreas" y URLs de un sitio.

Dos mecanismos complementarios:

    1. Sitemaps (sitemap.xml, sitemap_index.xml, y los declarados en
       robots.txt): la vía más completa y menos invasiva de enumerar
       las secciones/artículos de un sitio.
    2. Crawl BFS de enlaces internos con profundidad limitada, con
       priorización de URLs que "parecen contenido" (content_hints)
       y exclusión de rutas ruidosas (login, carrito, assets...).

Los enlaces a PDFs se detectan y se marcan para que el pipeline los
descargue y procese por la vía de `pdf_extractor`.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urldefrag

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("webharvest.discovery")


@dataclass
class CrawlTarget:
    url: str
    depth: int = 0
    hint_pdf: bool = False
    from_sitemap: bool = False
    from_listing: bool = False   # ítem descubierto en una página de listado


def normalize_url(base: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return None
    absolute = urljoin(base, href)
    absolute, _ = urldefrag(absolute)        # elimina #fragmentos
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        return None
    return absolute.rstrip("/") if parsed.path in ("", "/") else absolute


def bare_host(url: str) -> str:
    """Host sin "www.": www.chile.travel y chile.travel son el mismo sitio."""
    return urlparse(url).netloc.lower().removeprefix("www.")


def same_site(url: str, seed: str, follow_subdomains: bool,
              aliases: Iterable[str] = ()) -> bool:
    """¿`url` pertenece al sitio de `seed`?

    `www.` no cuenta: comparar el host exacto dejó en 0 a chile.travel y
    chileestuyo.cl, cuya semilla era www. y cuyos enlaces no lo llevan
    (las 20 secciones de cada uno se rechazaron como "offsite"). `aliases`
    son otros hosts del mismo sitio, sin www.: el pipeline agrega ahí el
    destino al que redirige la semilla (junji.gob.cl -> junji.cl).
    """
    a, b = bare_host(url), bare_host(seed)
    if a == b or a in aliases:
        return True
    if follow_subdomains:
        root = ".".join(b.split(".")[-2:])
        return a.endswith(root)
    return False


def is_query_trap(url: str) -> bool:
    """URLs que un CMS genera sin fin al re-aplicar su propio query string:
    /pagina?current=pdf?current=pdf, ...?a=1%3Fa%3D1, o el mismo parámetro
    repetido. Un museo Drupal produjo 449 variantes de 3 páginas (todas con
    el mismo texto): descargas inútiles que además inflan el libro de visitas.
    """
    q = urlparse(url).query
    if not q:
        return False
    if "?" in q or "%3f" in q.lower():
        return True
    names = [kv.split("=", 1)[0] for kv in q.split("&") if kv]
    return len(names) != len(set(names))


def is_excluded(url: str, patterns: list[str]) -> bool:
    """Patrones de exclusión del config.

    Un patrón de RUTA (empieza por "/" y termina en letra/dígito, p.ej.
    "/cart", "/login") excluye solo el segmento completo: /cart, /cart/x,
    /cart?y — NO /cartelera ni /loginformacion. Hallazgo del benchmark de
    cobertura: "/cart" (carrito de e-commerce) se tragaba toda la sección
    /cartelera de un museo (180 páginas nunca vistas). El resto de patrones
    (".jpg", "?share=", "/tag/", "/wp-admin") sigue siendo por subcadena.
    """
    low = url.lower()
    for p in patterns:
        p = p.lower()
        if p.startswith("/") and p[-1].isalnum():
            i = low.find(p)
            while i != -1:
                end = i + len(p)
                if end == len(low) or low[end] in "/?#":
                    return True
                i = low.find(p, i + 1)
        elif p in low:
            return True
    return False


def looks_like_content(url: str, hints: list[str]) -> bool:
    low = urlparse(url).path.lower()
    return any(h in low for h in hints)


# ----------------------------------------------------------------------
# Sitemaps
# ----------------------------------------------------------------------
def discover_sitemap_urls(seed: str, user_agent: str, limit: int = 2000) -> list[str]:
    """Lee robots.txt (Sitemap:) y ubicaciones convencionales."""
    base = f"{urlparse(seed).scheme}://{urlparse(seed).netloc}"
    candidates = [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]
    headers = {"User-Agent": user_agent}

    try:
        robots = httpx.get(f"{base}/robots.txt", headers=headers, timeout=10,
                           follow_redirects=True)
        if robots.status_code == 200:
            for line in robots.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    candidates.insert(0, line.split(":", 1)[1].strip())
    except Exception:
        pass

    found: list[str] = []
    seen_maps: set[str] = set()
    queue = deque(dict.fromkeys(candidates))       # dedupe conservando orden

    while queue and len(found) < limit:
        sm_url = queue.popleft()
        if sm_url in seen_maps:
            continue
        seen_maps.add(sm_url)
        try:
            resp = httpx.get(sm_url, headers=headers, timeout=15, follow_redirects=True)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
        except Exception:
            continue

        ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        if root.tag.endswith("sitemapindex"):
            for loc in root.iter(f"{ns}loc"):
                if loc.text:
                    queue.append(loc.text.strip())
        else:
            for loc in root.iter(f"{ns}loc"):
                if loc.text:
                    found.append(loc.text.strip())

    log.info("Sitemap de %s: %d URLs", base, len(found))
    return found[:limit]


# ----------------------------------------------------------------------
# Extracción de enlaces de una página ya descargada
# ----------------------------------------------------------------------
BINARY_DOC_EXTS = (".pdf", ".docx", ".doc", ".odt", ".rtf")

# Rutas de descarga sin extensión (DSpace: /bitstreams/<uuid>/download,
# CMSs varios: ?download=, /attachment/...). El content-type decide luego.
DOWNLOAD_HREF_PAT = re.compile(
    r"/download(/|$|\?)|/bitstreams?/|[?&](download|attachment|file)=|/attachments?/",
    re.I)


def looks_like_document(url: str) -> bool:
    return (url.lower().split("?")[0].endswith(BINARY_DOC_EXTS)
            or bool(DOWNLOAD_HREF_PAT.search(url)))


def extract_links(page_url: str, html: str) -> tuple[list[str], list[str]]:
    """Devuelve (links_html, links_documento) absolutos y desfragmentados.

    "Documento" = binario descargable con texto (PDF, Word, ODT...) por
    extensión o por ruta de descarga; van con hint para que el pipeline
    los priorice y despache por content-type real.
    """
    soup = BeautifulSoup(html, "lxml")
    html_links, doc_links = [], []
    for a in soup.find_all("a", href=True):
        url = normalize_url(page_url, a["href"])
        if not url:
            continue
        if looks_like_document(url):
            doc_links.append(url)
        else:
            html_links.append(url)
    return list(dict.fromkeys(html_links)), list(dict.fromkeys(doc_links))


# ----------------------------------------------------------------------
# Frontera de crawl (BFS priorizado)
# ----------------------------------------------------------------------
class Frontier:
    """Cola de URLs pendientes con deduplicación y prioridad por 'contenido'."""

    def __init__(self, cfg_discovery: dict, seen: set | None = None,
                 on_reject=None, site_hosts: set | None = None):
        # on_reject(url, motivo) se llama por cada URL vista y NO encolada
        # (fuera de dominio, profundidad, patrón excluido). Lo usa el libro
        # de visitas; los duplicados no se reportan (ruido sin información).
        self.on_reject = on_reject
        # hosts adicionales del sitio (sin www.). Es el set del pipeline, por
        # referencia: si la semilla redirige, el alias llega a la frontier ya
        # creada antes de que se encolen los enlaces de la portada.
        self.site_hosts = site_hosts if site_hosts is not None else set()
        self.max_depth = cfg_discovery["max_depth"]
        # exhaustivo: la profundidad deja de ser tope; los rieles son la
        # saturación (pipeline) y max_pages_per_site
        if cfg_discovery.get("exhaustive"):
            self.max_depth = 10 ** 6
        self.max_pages = cfg_discovery["max_pages_per_site"]
        self.follow_subdomains = cfg_discovery["follow_subdomains"]
        self.hints = cfg_discovery["content_hints"]
        self.excludes = cfg_discovery["exclude_patterns"]
        # Cinco niveles:
        #   docs     binarios descargables (activos hoja: baratos y completan
        #            un documento ya descubierto)
        #   items    detalles descubiertos en un listado (contenido confirmado
        #            por estructura, no solo por la pinta de la URL)
        #   high     la URL parece contenido (content_hints)
        #   priority sitemap
        #   normal   BFS
        self._docs: deque[CrawlTarget] = deque()
        self._items: deque[CrawlTarget] = deque()
        self._high: deque[CrawlTarget] = deque()
        self._priority: deque[CrawlTarget] = deque()
        self._normal: deque[CrawlTarget] = deque()
        # `seen` compartido permite que varios sub-crawls (p.ej. las
        # secciones de un mismo sitio) no re-visiten las mismas URLs.
        self._seen: set[str] = seen if seen is not None else set()
        self._emitted = 0

    def add(self, target: CrawlTarget, seed: str) -> bool:
        """Encola la URL. Devuelve True solo si era nueva y fue aceptada
        (lo usa la paginación sintetizada para validar que aporta ítems)."""
        url = target.url
        if url in self._seen:
            return False
        if target.depth > self.max_depth:
            return self._reject(url, "depth")
        if not same_site(url, seed, self.follow_subdomains,
                         self.site_hosts) and not target.hint_pdf:
            return self._reject(url, "offsite")
        if is_excluded(url, self.excludes):
            return self._reject(url, "excluded")
        if is_query_trap(url):
            return self._reject(url, "trap")
        self._seen.add(url)
        if target.hint_pdf:
            self._docs.append(target)
        elif target.from_listing:
            self._items.append(target)
        elif looks_like_content(url, self.hints):
            self._high.append(target)
        elif target.from_sitemap:
            self._priority.append(target)
        else:
            self._normal.append(target)
        return True

    def _reject(self, url: str, why: str) -> bool:
        # se marca como vista para no reportar el mismo rechazo N veces
        self._seen.add(url)
        if self.on_reject is not None:
            self.on_reject(url, why)
        return False

    def drain(self) -> list[CrawlTarget]:
        """Vacía la frontier y devuelve lo que quedó sin visitar (para que
        el cierre del crawl lo anote como not_fetched)."""
        left: list[CrawlTarget] = []
        for q in (self._docs, self._items, self._high, self._priority, self._normal):
            left.extend(q)
            q.clear()
        return left

    def next(self) -> CrawlTarget | None:
        if self._emitted >= self.max_pages:
            return None
        q = (self._docs or self._items or self._high
             or self._priority or self._normal)
        if not q:
            return None
        self._emitted += 1
        return q.popleft()

    def __len__(self) -> int:
        return (len(self._docs) + len(self._items) + len(self._high)
                + len(self._priority) + len(self._normal))
