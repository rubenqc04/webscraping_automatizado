"""Descubrimiento de secciones de un sitio a partir de su portada.

Dado el HTML del home, identifica las secciones de contenido (Nacional,
Deportes, Publicaciones, Ciencia...) para poder profundizar en cada una
con presupuesto propio. Heurísticas independientes del sitio:

  - Los enlaces de sección viven en landmarks de navegación (<nav>,
    <header>, contenedores con clase nav/menu) o en el propio home.
  - Apuntan al mismo sitio, con rutas cortas (1-2 segmentos) y texto de
    ancla breve tipo etiqueta ("País", "Economía"), no titulares largos.
  - Se descarta la navegación utilitaria (login, contacto, ayuda,
    términos, redes sociales...) por vocabulario multilingüe.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

log = logging.getLogger("webharvest.discovery.sections")

# navegación utilitaria, no secciones de contenido (es/en/pt)
UTILITY_PAT = re.compile(
    r"\b(login|log\s?in|sign\s?(in|up)|registr|cuenta|account|perfil|profile"
    r"|contact|contacto|ayuda|help|soporte|support|faq|acerca|about|quienes"
    r"|t[eé]rminos|terms|privacidad|privacy|cookies|legal|mapa\s+del\s+sitio"
    r"|sitemap|suscr|subscri|newsletter|publicidad|advertis|rss|podcast"
    r"|facebook|twitter|instagram|youtube|tiktok|linkedin|whatsapp"
    r"|buscar|search|idioma|language|english|espa[ñn]ol|portugu[eê]s"
    r"|inicio|home|portada)\b", re.I)

UTILITY_PATH_PAT = re.compile(
    r"/(login|signin|account|cuenta|contact|about|ayuda|help|terms|privac"
    r"|legal|search|buscar|tag|etiqueta|rss|feed|sitemap)\b", re.I)

# secciones de solo-media: válidas, pero con poco texto extraíble — van al
# final del ranking para que el presupuesto priorice secciones de texto
MEDIA_PAT = re.compile(
    r"\b(videos?|multimedia|audios?|fotos?|im[aá]gene?s|galer[ií]as?|gallery"
    r"|photos?|podcasts?|programas?|radio|tv|en\s+vivo|live|streaming)\b", re.I)


@dataclass
class Section:
    name: str
    url: str
    source: str          # nav | header | body
    path_depth: int
    is_media: bool = False


def _in_navigation(a: Tag) -> tuple[bool, str]:
    node = a
    while isinstance(node, Tag):
        if node.name in ("nav",):
            return True, "nav"
        if node.name in ("header",):
            return True, "header"
        classes = " ".join(node.get("class") or []) + " " + (node.get("id") or "")
        if re.search(r"\b(nav|menu|sections?|categorias?|channels?)\b", classes, re.I):
            return True, "nav"
        node = node.parent
    return False, "body"


def discover_sections(url: str, html: str, max_sections: int = 12) -> list[Section]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg", "footer"]):
        t.decompose()

    page = urlparse(url)
    seen_paths: set[str] = set()
    sections: list[Section] = []

    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ").split())
        if not (2 <= len(text) <= 40):          # etiqueta corta, no titular
            continue
        if UTILITY_PAT.search(text):
            continue
        target = urlparse(urljoin(url, a["href"].strip()))
        if target.netloc.lower() != page.netloc.lower():
            continue
        path = target.path.rstrip("/")
        if not path or path == page.path.rstrip("/"):
            continue
        if UTILITY_PATH_PAT.search(path) or target.fragment:
            continue
        segments = [s for s in path.split("/") if s]
        if len(segments) > 3:                   # secciones = rutas cortas
            continue
        # el último segmento debe parecer un nombre, no un id/artículo
        last = segments[-1]
        if re.search(r"\d{4,}|\.(html?|php|aspx?)$", last, re.I) and len(segments) > 1:
            continue
        in_nav, source = _in_navigation(a)
        if not in_nav:
            continue
        if path.lower() in seen_paths:
            continue
        seen_paths.add(path.lower())
        is_media = bool(MEDIA_PAT.search(text) or MEDIA_PAT.search(path))
        sections.append(Section(name=text, url=target._replace(fragment="").geturl(),
                                source=source, path_depth=len(segments),
                                is_media=is_media))

    # texto antes que media; rutas cortas primero; estable por aparición
    sections.sort(key=lambda s: (s.is_media, s.path_depth))
    if len(sections) > max_sections:
        log.info("Secciones halladas: %d; se toman las primeras %d",
                 len(sections), max_sections)
    return sections[:max_sections]
