"""Mapa semántico genérico de una página (sin selectores por sitio).

Responde cuatro preguntas sobre cualquier HTML ya obtenido (estático o
renderizado):

    1. ¿Qué secciones tiene la página y dónde se concentra el texto?
       -> `sections` (landmarks header/nav/main/aside/footer con su cuota
          de texto) y `content_root` (el contenedor más profundo que aún
          concentra la mayoría del texto visible).
    2. ¿Es un LISTADO (serie de ítems repetidos con enlace a detalle),
       un ARTÍCULO (bloque de texto continuo) o una página de NAVEGACIÓN?
       -> `page_kind`, decidido con señales estructurales:
          grupos de hermanos repetidos (misma etiqueta+clases), párrafo
          dominante y densidad de enlaces.
    3. Si es listado: ¿cuáles son los ítems (título, URL de detalle,
       snippet/resumen) y cómo se pagina?
       -> `listing_items`, `pagination_urls`, `pagination_js_only`.
    4. Si es artículo: ¿qué enlaces llevan al texto completo (HTML o PDF)?
       -> `fulltext_links`.

Todo son heurísticas independientes del sitio: contenedores repetidos,
concentración de texto y vocabulario multilingüe de "texto completo".
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from bs4.element import Tag

log = logging.getLogger("webharvest.analysis")

# --- vocabulario multilingüe (es/en/pt) ---------------------------------
FULLTEXT_TEXT_PAT = re.compile(
    r"full\s*-?\s*text|texto\s+completo|texto\s+integral|read\s+(the\s+)?article"
    r"|ver\s+documento|download|descargar|\bpdf\b", re.I)
PDF_HREF_PAT = re.compile(
    r"\.pdf(\b|$)|blobtype=pdf|render.*pdf|/pdf(/|$|\?)|format=pdf|type=pdf", re.I)
# "Siguiente" puede venir sola, acompañada ("Página siguiente", "Siguiente »")
# o como invitación a cargar más ("Ver más", "Cargar más", "Load more"). Antes
# el patrón estaba anclado a toda la cadena y solo cazaba la forma desnuda.
NEXT_TEXT_PAT = re.compile(
    r"^\s*(?:(?:p[áa]gina|pag\.?|p[áa]g\.?)\s+)?"
    r"(next(?:\s+page)?|siguiente[s]?|pr[oó]xima?|seguinte|despu[ée]s|depois"
    r"|more results|m[áa]s resultados"
    r"|ver\s+m[áa]s|cargar\s+m[áa]s|mostrar\s+m[áa]s|load\s+more|view\s+more"
    r"|ver\s+siguiente"
    r"|>|>>|›|››|»|→)"
    r"(?:\s*(?:p[áa]gina|page|»|›|>|→))?\s*$", re.I)
# "Ver más", "Leer más", "View more" son también el enlace de CADA ítem de un
# listado ("read more" de WordPress/Elementor). Un control por ítem aparece
# muchas veces en la página; un enlace de página siguiente, una o dos. Ese
# recuento es lo que los distingue: sin él, los 10 "Ver más »" de un listado
# de Elementor entraban como si fueran 10 páginas siguientes.
AMBIGUOUS_NEXT_PAT = re.compile(
    r"^\s*(ver|cargar|mostrar|load|view|leer|read)\s+(m[áa]s|more)\s*[»›>→]?\s*$", re.I)
MAX_AMBIGUOUS_REPEATS = 3
# Nombres de parámetro de paginación en los gestores más vistos: page (Drupal),
# paged (WordPress clásico), start (Joomla/DSpace), pageNumber (.NET), pg, pagina.
PAGE_PARAM_NAMES = ("page", "paged", "pagina", "pag", "pagenumber", "pagenum",
                    "page_num", "pageindex", "pg", "p", "start", "offset",
                    "from", "first", "desde", "inicio", "seite")
PAGE_PARAM_PAT = re.compile(
    r"[?&](" + "|".join(PAGE_PARAM_NAMES) + r")=\d+", re.I)
# Enlaces cuyo texto es solo un número: "1 2 3 4 › " es paginación sea cual sea
# la convención de URL (parámetro desconocido o número en la ruta, /noticias/2).
NUMBER_TEXT_PAT = re.compile(r"^\s*\d{1,4}\s*$")
# Paginación por segmento de ruta: /page/2/, /pagina/3, /pag/4
PAGE_PATH_PAT = re.compile(r"/(page|pagina|p[áa]g|pag|seite)/\d+/?$", re.I)
# Solo vocabulario de PAGINADOR. "btn-next" y "siguiente" quedaron fuera a
# propósito: son estilos de botón que Cooperativa usa también dentro de sus
# artículos, y marcaban como "listado con paginador JS" a notas sueltas, que
# entonces sintetizaban /page/2/ sobre la URL del artículo.
PAGINATION_CLASS_PAT = re.compile(
    r"pagin|pager|infinite-scroll|load-?more|cargar-?mas|ver-?mas|show-?more"
    r"|view-?more|next-?page", re.I)
NAV_ANCESTOR_TAGS = {"nav", "header", "footer"}

# umbrales por defecto (sobrescribibles vía cfg["analysis"])
DEFAULTS = {
    "min_group_items": 4,            # mínimo de ítems repetidos para considerar listado
    "min_item_chars": 40,            # texto mínimo por ítem (descarta menús)
    "min_group_link_share": 0.6,     # fracción de ítems con enlace
    "min_group_text_share": 0.4,     # cuota del texto TOTAL de la página en el grupo:
                                     # distingue el listado principal (domina la página)
                                     # de listas embebidas (referencias, "similares")
    "min_group_score": 0.5,          # descarta menús (penalizados) y grupos débiles
    "dominant_paragraph_chars": 700, # un párrafo así de largo => página de artículo
    "max_snippet_chars": 500,
}


# ------------------------------------------------------------------------
@dataclass
class ListingItem:
    title: str
    url: str
    snippet: str


@dataclass
class PageMap:
    url: str
    page_kind: str                       # listing | article | navigation | empty
    total_text_chars: int
    content_root: dict                   # {selector, text_chars, text_share}
    sections: list[dict]                 # landmarks + cuota de texto
    best_group: Optional[dict]           # grupo repetido ganador (diagnóstico)
    listing_items: list[ListingItem] = field(default_factory=list)
    pagination_urls: list[str] = field(default_factory=list)
    pagination_js_only: bool = False
    fulltext_links: list[dict] = field(default_factory=list)
    dominant_paragraph_chars: int = 0
    link_density: float = 0.0            # chars dentro de <a> / chars totales

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------------
def _text(el: Tag) -> str:
    return " ".join(el.get_text(" ").split())


def _selector(el: Tag) -> str:
    parts = []
    node = el
    while isinstance(node, Tag) and node.name not in ("html", "[document]"):
        seg = node.name
        if node.get("id"):
            seg += f"#{node['id']}"
            parts.append(seg)
            break
        classes = node.get("class") or []
        if classes:
            seg += "." + ".".join(classes[:2])
        parts.append(seg)
        node = node.parent
    return " > ".join(reversed(parts[:4]))


def _in_nav(el: Tag) -> bool:
    node = el
    while isinstance(node, Tag):
        if node.name in NAV_ANCESTOR_TAGS:
            return True
        classes = " ".join(node.get("class") or [])
        if re.search(r"\b(nav|menu|breadcrumb|footer|header)\b", classes, re.I):
            return True
        node = node.parent
    return False


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg", "template"]):
        t.decompose()
    return soup


# ------------------------------------------------------------------------
# 1) Secciones y concentración de contenido
# ------------------------------------------------------------------------
def _landmark_sections(soup: BeautifulSoup, total: int) -> list[dict]:
    sections = []
    for tag in ("header", "nav", "main", "article", "aside", "footer", "form"):
        for el in soup.find_all(tag):
            chars = len(_text(el))
            if chars == 0:
                continue
            sections.append({
                "role": tag,
                "selector": _selector(el),
                "text_chars": chars,
                "text_share": round(chars / total, 3) if total else 0.0,
            })
    sections.sort(key=lambda s: -s["text_chars"])
    return sections[:12]


def _find_content_root(body: Tag, total: int) -> Tag:
    """Desciende mientras un único hijo concentre >=60% del texto del padre."""
    node = body
    while True:
        node_chars = len(_text(node)) or 1
        best_child, best_chars = None, 0
        for child in node.find_all(True, recursive=False):
            c = len(_text(child))
            if c > best_chars:
                best_child, best_chars = child, c
        if best_child is not None and best_chars / node_chars >= 0.6:
            node = best_child
            continue
        return node


# ------------------------------------------------------------------------
# 2) Grupos de hermanos repetidos (candidatos a listado)
# ------------------------------------------------------------------------
def _repeated_groups(root: Tag, opts: dict) -> list[dict]:
    groups = []
    # incluye al propio root: puede ser él mismo el contenedor del listado
    for parent in [root, *root.find_all(True)]:
        by_sig: dict[tuple, list[Tag]] = {}
        by_tag: dict[str, list[Tag]] = {}
        for child in parent.find_all(True, recursive=False):
            sig = (child.name, tuple(sorted(child.get("class") or [])))
            by_sig.setdefault(sig, []).append(child)
            by_tag.setdefault(child.name, []).append(child)

        # Fallback por tag: los temas de bloques (WordPress y similares) dan
        # a cada ítem clases únicas (post-123, category-x...), así que la
        # firma exacta nunca agrupa. Si ningún grupo por clases alcanza 3
        # miembros para un tag que se repite >= 3 veces, se agrupa por tag.
        for tag_name, members in by_tag.items():
            if len(members) >= 3 and not any(
                    len(m) >= 3 for (t, _), m in by_sig.items() if t == tag_name):
                by_sig[(tag_name, None)] = members

        for (tag, classes), members in by_sig.items():
            if len(members) < 3 or tag in ("br", "hr", "meta", "link", "option"):
                continue
            texts = [len(_text(m)) for m in members]
            mean_chars = statistics.mean(texts)
            if mean_chars < opts["min_item_chars"]:
                continue
            with_link = sum(1 for m in members if m.find("a", href=True))
            link_share = with_link / len(members)
            homogeneity = 1.0
            if len(texts) > 1 and mean_chars:
                homogeneity = max(0.0, 1 - statistics.pstdev(texts) / (mean_chars * 2))
            nav_penalty = 0.4 if _in_nav(parent) else 1.0
            score = nav_penalty * (
                0.35 * min(len(members) / 10, 1.0)
                + 0.30 * link_share
                + 0.20 * min(mean_chars / 300, 1.0)
                + 0.15 * homogeneity
            )
            groups.append({
                "selector": f"{parent.name} > {tag}"
                            + ("." + ".".join(classes) if classes else ""),
                "n_items": len(members),
                "mean_chars": round(mean_chars, 1),
                "link_share": round(link_share, 2),
                "homogeneity": round(homogeneity, 2),
                "total_chars": sum(texts),
                "score": round(score, 3),
                "_members": members,
            })
    groups.sort(key=lambda g: -g["score"])
    return groups


HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5"}


def _dominant_anchor(item: Tag, page_url: str,
                     shared_hrefs: frozenset = frozenset()) -> Optional[Tag]:
    """El enlace 'título' del ítem.

    No es simplemente el de texto más largo: en una tarjeta de evento el
    enlace al recinto ("Museo Regional de la Araucanía") es más largo que
    el título ("Taller de mosaicos"), y como el recinto se repite en varias
    tarjetas, la deduplicación por URL tiraba el evento (benchmark: 3 de 12
    ítems perdidos por página de cartelera). Señales, en orden:
      1. el href NO se repite en otras tarjetas del grupo (un facetado,
         recinto o categoría compartido no es el ítem);
      2. el mismo href aparece 2+ veces en la tarjeta (imagen + título);
      3. el enlace vive dentro de un heading;
      4. a igualdad, el texto más largo.
    """
    page_path = urlparse(page_url).path
    counts: dict[str, int] = {}
    cands = []
    for a in item.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        target = urlparse(urljoin(page_url, href))
        # descarta enlaces facetados que vuelven a la misma ruta (p.ej. /search?...)
        if target.path == page_path:
            continue
        key = urljoin(page_url, href)
        counts[key] = counts.get(key, 0) + 1
        cands.append((a, key))
    best, best_score = None, None
    for a, key in cands:
        score = (0 if key in shared_hrefs else 100_000) \
            + (10_000 if counts[key] >= 2 else 0) \
            + (1_000 if a.find_parent(list(HEADING_TAGS)) is not None else 0) \
            + len(_text(a))
        if best_score is None or score > best_score:
            best, best_score = a, score
    return best


def _extract_listing_items(group: dict, page_url: str, opts: dict) -> list[ListingItem]:
    # hrefs que aparecen en más de una tarjeta: facetas, recintos, categorías
    per_member: list[set[str]] = []
    for member in group["_members"]:
        hs = set()
        for a in member.find_all("a", href=True):
            href = a["href"].strip()
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                hs.add(urljoin(page_url, href))
        per_member.append(hs)
    freq: dict[str, int] = {}
    for hs in per_member:
        for h in hs:
            freq[h] = freq.get(h, 0) + 1
    shared = frozenset(h for h, n in freq.items() if n > 1)

    items, seen = [], set()
    for member in group["_members"]:
        anchor = _dominant_anchor(member, page_url, shared)
        if anchor is None:
            continue
        url = urljoin(page_url, anchor["href"].strip())
        if url in seen:
            continue
        seen.add(url)
        title = _text(anchor)
        snippet = _text(member)[: opts["max_snippet_chars"]]
        items.append(ListingItem(title=title, url=url, snippet=snippet))
    return items


# ------------------------------------------------------------------------
# 3) Paginación
# ------------------------------------------------------------------------
def _numbered_pagination(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Enlaces numerados consecutivos ("1 2 3 4") = paginación.

    Es la vía agnóstica: cubre convenciones de URL que ningún patrón de
    parámetro conoce (número en la ruta /noticias/2, ?pageIndex=, ?nro=).
    Se exigen 3+ números distintos y consecutivos bajo un mismo padre, para
    no confundir un archivo por años ("2024 2023 2022" no es consecutivo
    ascendente desde 1... pero sí lo es descendente, de ahí el tope de 4
    dígitos y el requisito de que el mínimo sea <= 3).
    """
    best: list[str] = []
    for parent in soup.find_all(True):
        nums: list[tuple[int, str]] = []
        for a in parent.find_all("a", href=True, recursive=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:")):
                continue
            t = _text(a) or ""
            if NUMBER_TEXT_PAT.match(t):
                nums.append((int(t.strip()), urljoin(page_url, href)))
        if len(nums) < 3:
            continue
        vals = sorted({n for n, _ in nums})
        # consecutivos y empezando bajo: un paginador real es 1,2,3... no años
        if min(vals) > 3 or vals != list(range(min(vals), min(vals) + len(vals))):
            continue
        urls = list(dict.fromkeys(u for _, u in sorted(nums)))
        if len(urls) > len(best):
            best = urls
    return best


def _find_pagination(soup: BeautifulSoup, page_url: str) -> tuple[list[str], bool]:
    anchors = [a for a in soup.find_all("a", href=True)
               if a["href"].strip()
               and not a["href"].strip().startswith(("#", "javascript:"))]
    # cuántas veces se repite cada etiqueta ambigua ("Ver más"): si son muchas,
    # es el enlace de cada ítem, no la página siguiente
    repeats: dict[str, int] = {}
    for a in anchors:
        t = (_text(a) or "").strip().lower()
        if AMBIGUOUS_NEXT_PAT.match(t):
            repeats[t] = repeats.get(t, 0) + 1

    page_host = urlparse(page_url).netloc.lower().removeprefix("www.")
    urls: list[str] = []
    for a in anchors:
        href = a["href"].strip()
        target = urljoin(page_url, href)
        # La paginación de un listado vive en el MISMO host: un rel=next que
        # apunta afuera es una fuga de configuración (un minsal.cl cuyo
        # rel=next señalaba a su servidor de pruebas), no la página 2.
        if urlparse(target).netloc.lower().removeprefix("www.") != page_host:
            continue
        text = (_text(a) or "").strip()
        rel = " ".join(a.get("rel") or [])
        by_text = bool(NEXT_TEXT_PAT.match(text))
        if by_text and AMBIGUOUS_NEXT_PAT.match(text.lower()) and \
                repeats.get(text.lower(), 0) > MAX_AMBIGUOUS_REPEATS:
            by_text = False
        if ("next" in rel.lower() or by_text
                or PAGE_PARAM_PAT.search(href)
                or PAGE_PATH_PAT.search(urlparse(target).path)):
            urls.append(target)
    if not urls:
        urls = _numbered_pagination(soup, page_url)
    urls = list(dict.fromkeys(urls))

    js_only = False
    if not urls:
        for el in soup.find_all(True, class_=PAGINATION_CLASS_PAT):
            if not el.find("a", href=True):
                js_only = True
                break
    return urls, js_only


# Parámetros que cuentan PÁGINAS (se incrementan en 1) y los que cuentan
# ELEMENTOS SALTADOS (se incrementan en el tamaño de página observado).
# "p" incluido: es la convención de gob.cl, cuya paginación una corrida real
# recorrió hasta p=1545. Sin él, la síntesis añadía un segundo p=2 y producía
# ?p=2&p=2, que el detector de trampas descartaba (petición desperdiciada).
PAGE_INDEX_PARAMS = ("page", "paged", "pagina", "pag", "pagenumber", "pagenum",
                     "page_num", "pageindex", "pg", "p", "seite")
OFFSET_PARAMS = ("start", "offset", "from", "first", "desde", "inicio")


def synthesize_next_page_urls(url: str) -> list[str]:
    """Convenciones de "página siguiente" para sondear y validar.

    - Parámetro de índice:  ?page=N / ?paged=N / ?pg=N  -> N+1
    - Parámetro de salto:   ?start=20 / ?offset=20      -> 40 (paso = valor)
    - Segmento de ruta:     /page/N/ -> N+1, o añade /page/2/ (WordPress)
    - URL desnuda:          añade /page/2/ y ?page=2

    Si la URL YA venía paginada por un mecanismo, solo se continúa ese: no
    tiene sentido pedir ?paged=2&page=2. El resultado SIEMPRE se valida: si
    la página sintetizada no aporta ítems nuevos, no se sintetiza la
    siguiente.
    """
    parts = urlparse(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    lower = {k.lower() for k, _ in params}

    # a) ya paginada por parámetro: incrementar ESE parámetro
    for name in PAGE_INDEX_PARAMS + OFFSET_PARAMS:
        if name not in lower:
            continue
        out = []
        for k, v in params:
            if k.lower() == name and v.isdigit():
                n = int(v)
                if name in OFFSET_PARAMS:
                    if n <= 0:            # paso desconocido: no adivinar
                        return []
                    out.append((k, str(n * 2)))
                else:
                    out.append((k, str(n + 1)))
            else:
                out.append((k, v))
        return [urlunparse(parts._replace(query=urlencode(out)))]

    # b) ya paginada por ruta /page/N/: incrementar el número
    m = re.search(r"^(.*/(?:page|pagina|p[áa]g|pag|seite)/)(\d+)(/?)$",
                  parts.path, re.I)
    if m:
        new_path = f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}"
        return [urlunparse(parts._replace(path=new_path))]

    # c) URL desnuda: probar las convenciones más extendidas. Se validan
    # todas contra "aportó ítems nuevos", así que probar tres es barato;
    # ?p=N es la de gob.cl, que en una corrida real llegó a p=1545.
    base = parts.path if parts.path.endswith("/") else parts.path + "/"
    return [
        urlunparse(parts._replace(path=f"{base}page/2/")),
        urlunparse(parts._replace(query=urlencode(params + [("page", "2")]))),
        urlunparse(parts._replace(query=urlencode(params + [("p", "2")]))),
    ]


def synthesize_next_page_url(url: str) -> Optional[str]:
    """Compatibilidad: el candidato con parámetro `page` (uso previo).

    Se elige por nombre y no por posición: la lista de candidatas creció
    (ahora incluye ?p=N) y "el último" dejó de ser el que esta función
    prometía.
    """
    for cand in synthesize_next_page_urls(url):
        if re.search(r"[?&]page=\d+", cand):
            return cand
    urls = synthesize_next_page_urls(url)
    return urls[-1] if urls else None


# ------------------------------------------------------------------------
# 4) Enlaces de texto completo (en páginas de artículo/detalle)
# ------------------------------------------------------------------------
FULLTEXT_HEADING_PAT = re.compile(
    r"full\s*-?\s*text|texto\s+completo|texto\s+integral|documento\s+completo", re.I)


def _fulltext_scopes(soup: BeautifulSoup) -> list[Tag]:
    """Contenedores bajo un heading tipo 'Full text (links)' / 'Texto completo'.

    Si la página declara una sección así, sus enlaces son mucho más
    fiables que un barrido global (que arrastra menús y referencias).
    """
    scopes = []
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        if FULLTEXT_HEADING_PAT.search(_text(h)):
            parent = h.parent
            if isinstance(parent, Tag) and parent.find("a", href=True):
                scopes.append(parent)
    return scopes


def _find_fulltext_links(soup: BeautifulSoup, page_url: str) -> list[dict]:
    page_host = urlparse(page_url).netloc.lower().removeprefix("www.")
    scopes = _fulltext_scopes(soup)
    anchors = [a for scope in scopes for a in scope.find_all("a", href=True)]
    scoped = bool(anchors)
    if not scoped:
        anchors = soup.find_all("a", href=True)
    found, seen = [], set()
    for a in anchors:
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        text = _text(a)
        is_pdf = bool(PDF_HREF_PAT.search(href)) or bool(re.search(r"\bpdf\b", text, re.I))
        is_ft = bool(FULLTEXT_TEXT_PAT.search(text))
        # rutas de descarga sin extensión (p.ej. /bitstreams/<id>/download):
        # candidatas a documento; el content-type decide al descargar
        is_dl = bool(re.search(
            r"/download(/|$|\?)|/bitstreams?/|[?&](download|attachment|file)=", href, re.I))
        # dentro de una sección 'Full text' declarada, todo enlace cuenta;
        # en el barrido global se exige coincidencia de vocabulario o descarga
        if not scoped and not (is_pdf or is_ft or is_dl):
            continue
        url = urljoin(page_url, href)
        if url in seen or url == page_url:
            continue
        # misma ruta sin fragmento = auto-enlace; con fragmento distinto puede
        # ser una vista SPA (p.ej. "#free-full-text") que sí renderiza contenido
        if (urlparse(url).path == urlparse(page_url).path
                and urlparse(url).fragment == urlparse(page_url).fragment):
            continue
        seen.add(url)
        found.append({
            "url": url,
            "anchor_text": text[:120],
            "kind": "pdf" if is_pdf else ("download" if is_dl else "fulltext"),
            "same_site": urlparse(url).netloc.lower().removeprefix("www.") == page_host,
        })
    # PDFs primero: son la vía de extracción más fiable
    found.sort(key=lambda d: (d["kind"] != "pdf", not d["same_site"]))
    return found


# ------------------------------------------------------------------------
# Orquestación
# ------------------------------------------------------------------------
def build_page_map(url: str, html: str, cfg_analysis: Optional[dict] = None) -> PageMap:
    opts = {**DEFAULTS, **(cfg_analysis or {})}
    soup = _clean_soup(html)
    body = soup.body or soup

    total = len(_text(body))
    if total < 50:
        return PageMap(url=url, page_kind="empty", total_text_chars=total,
                       content_root={}, sections=[], best_group=None)

    sections = _landmark_sections(soup, total)
    content_root = _find_content_root(body, total)
    root_chars = len(_text(content_root)) or 1
    anchor_chars = sum(len(_text(a)) for a in content_root.find_all("a", href=True))
    link_density = round(min(anchor_chars / root_chars, 1.0), 3)

    # párrafo/bloque de texto continuo más largo (señal de artículo)
    dominant_par = 0
    for el in content_root.find_all(["p", "div", "section", "blockquote"]):
        own = len(" ".join(
            s.strip() for s in el.find_all(string=True, recursive=False)))
        dominant_par = max(dominant_par, own)

    groups = _repeated_groups(content_root, opts)
    best = groups[0] if groups else None

    listing_items: list[ListingItem] = []
    page_kind = "navigation"
    if best:
        group_share = best["total_chars"] / (total or 1)
        is_listing = (best["n_items"] >= opts["min_group_items"]
                      and best["score"] >= opts["min_group_score"]
                      and best["link_share"] >= opts["min_group_link_share"]
                      and group_share >= opts["min_group_text_share"]
                      and dominant_par < opts["dominant_paragraph_chars"])
        # Layouts "revista" (portadas de noticias, homes): la página no ES
        # un listado puro, pero contiene un grupo fuerte de titulares con
        # enlaces. Los ítems se extraen igual para que el crawl profundice.
        has_strong_group = (best["n_items"] >= 5
                            and best["score"] >= 0.65
                            and best["link_share"] >= 0.8)
        if is_listing or has_strong_group:
            listing_items = _extract_listing_items(best, url, opts)
            if is_listing and len(listing_items) >= 3:
                page_kind = "listing"

    if page_kind != "listing":
        if dominant_par >= opts["dominant_paragraph_chars"] or (
                root_chars >= 1200 and link_density < 0.5):
            page_kind = "article"

    # La paginación se busca SIEMPRE que la página tenga algo que paginar,
    # no solo cuando se clasifica como "listing": las portadas tipo revista
    # y los repositorios DSpace extraen ítems por la vía del "grupo fuerte"
    # y su page_kind queda en article/navigation, así que su paginador nunca
    # se miraba. Era el vacío que dejaba a Cooperativa, Currículum Nacional
    # y la Biblioteca Digital del Mineduc en una sola página.
    pagination_urls, js_only = ([], False)
    if page_kind != "empty":
        pagination_urls, js_only = _find_pagination(soup, url)

    fulltext_links = _find_fulltext_links(soup, url) if page_kind == "article" else []

    best_public = None
    if best:
        best_public = {k: v for k, v in best.items() if not k.startswith("_")}

    pmap = PageMap(
        url=url,
        page_kind=page_kind,
        total_text_chars=total,
        content_root={
            "selector": _selector(content_root),
            "text_chars": root_chars,
            "text_share": round(root_chars / total, 3),
        },
        sections=sections,
        best_group=best_public,
        listing_items=listing_items,
        pagination_urls=pagination_urls,
        pagination_js_only=js_only,
        fulltext_links=fulltext_links,
        dominant_paragraph_chars=dominant_par,
        link_density=link_density,
    )
    log.info("PageMap %s: kind=%s items=%d fulltext=%d root=%s (%.0f%% del texto)",
             url, page_kind, len(listing_items), len(fulltext_links),
             pmap.content_root["selector"], 100 * pmap.content_root["text_share"])
    return pmap
