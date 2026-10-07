"""Extracción de contenido principal desde HTML.

Pipeline de extracción en cascada:

    1. trafilatura        -> el mejor extractor genérico de "main content"
                             (noticias, blogs, gob, etc.). Devuelve texto
                             + metadata (título, autor, fecha, idioma...).
    2. Heurística propia  -> si trafilatura falla, se buscan contenedores
                             semánticos (<article>, <main>, [role=main],
                             .post-content, etc.) y se convierte con
                             markdownify.
    3. Fallback bruto     -> texto visible del <body> limpiado.

Además se raspa metadata estructurada del <head>:
JSON-LD (schema.org Article/NewsArticle/ScholarlyArticle), Open Graph,
Dublin Core y meta tags de citación académica (citation_*, muy comunes
en repositorios científicos y revistas).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

import trafilatura
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from ..models import (ContentCategory, Document, DocumentMetadata,
                      SourceType, count_words, make_doc_id)

log = logging.getLogger("webharvest.extract.html")

SEMANTIC_SELECTORS = [
    "article", "main", "[role=main]",
    ".post-content", ".entry-content", ".article-body", ".article__body",
    ".story-body", "#content article", ".nota", ".cuerpo-nota",
]

NOISE_SELECTORS = [
    "nav", "header", "footer", "aside", "form", "iframe",
    ".sidebar", ".related", ".comments", ".share", ".newsletter",
    ".cookie", ".advert", ".ad", ".breadcrumb", ".menu",
]


# ----------------------------------------------------------------------
# Metadata estructurada del <head>
# ----------------------------------------------------------------------
def _parse_jsonld(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                candidates.extend(x for x in graph if isinstance(x, dict))
                continue
            t = str(item.get("@type", "")).lower()
            if any(k in t for k in ("article", "report", "posting", "webpage")):
                return item
    return {}


def _meta(soup: BeautifulSoup, *names: str) -> Optional[str]:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def scrape_head_metadata(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    jsonld = _parse_jsonld(soup)

    def jl(key: str) -> Optional[str]:
        val = jsonld.get(key)
        if isinstance(val, dict):
            return val.get("name")
        if isinstance(val, list):
            return ", ".join(v.get("name", str(v)) if isinstance(v, dict) else str(v) for v in val)
        return val

    authors: list[str] = []
    # Meta tags académicos (Highwire/Google Scholar): citation_author repetido
    for tag in soup.find_all("meta", attrs={"name": "citation_author"}):
        if tag.get("content"):
            authors.append(tag["content"].strip())
    if not authors and jl("author"):
        authors = [a.strip() for a in str(jl("author")).split(",") if a.strip()]
    if not authors:
        a = _meta(soup, "article:author", "author", "dc.creator", "DC.creator")
        if a:
            authors = [a]

    keywords_raw = _meta(soup, "keywords", "news_keywords", "citation_keywords") or ""
    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

    return {
        # og:title/citation antes que JSON-LD: algunos sitios (Wikipedia)
        # ponen su "descripción corta" en el headline del JSON-LD
        "title": _meta(soup, "og:title", "citation_title", "twitter:title", "dc.title")
                 or jl("headline")
                 or (soup.title.get_text(strip=True) if soup.title else None),
        "description": jl("description") or _meta(soup, "og:description", "description", "twitter:description"),
        "published_date": jsonld.get("datePublished")
                          or _meta(soup, "article:published_time", "citation_publication_date",
                                   "citation_date", "dc.date", "date", "publish-date"),
        "authors": authors,
        "site_name": _meta(soup, "og:site_name") or (jsonld.get("publisher") or {}).get("name")
                     if isinstance(jsonld.get("publisher"), dict) else _meta(soup, "og:site_name"),
        "language": (soup.html.get("lang") if soup.html else None) or _meta(soup, "og:locale", "dc.language"),
        "keywords": keywords,
        "canonical": (soup.find("link", rel="canonical") or {}).get("href")
                     if soup.find("link", rel="canonical") else None,
        "has_citation_meta": bool(soup.find("meta", attrs={"name": re.compile("^citation_")})),
        # Derechos de uso declarados por la propia página (evidencia para
        # curación del corpus): meta de copyright/rights y licencia enlazada
        # (rel=license, JSON-LD license, Creative Commons).
        "rights": _meta(soup, "copyright", "rights", "dc.rights", "DC.rights",
                        "dcterms.rights"),
        "license_url": _find_license_url(soup, jsonld),
    }


def _find_license_url(soup: BeautifulSoup, jsonld: dict) -> Optional[str]:
    lic = jsonld.get("license")
    if isinstance(lic, dict):
        lic = lic.get("url") or lic.get("@id")
    if isinstance(lic, str) and lic.startswith("http"):
        return lic
    for el in soup.find_all(["link", "a"], rel=True):
        rels = [str(x).lower() for x in (el.get("rel") or [])]
        if "license" in rels and el.get("href"):
            return el["href"]
    a = soup.find("a", href=re.compile(r"creativecommons\.org/licenses"))
    return a["href"] if a else None


# ----------------------------------------------------------------------
# Clasificación gruesa del tipo de fuente
# ----------------------------------------------------------------------
GOV_TLD_HINTS = (".gov", ".gob.", ".gub.", ".gov.", ".mil")
SCI_DOMAIN_HINTS = ("doi.org", "arxiv", "scielo", "pubmed", "springer", "elsevier",
                    "sciencedirect", "nature.com", "mdpi", "redalyc", "researchgate",
                    "academic.oup", "jstor", "wiley")
NEWS_HINTS = ("noticia", "news", "diario", "periodico", "prensa", "clarin", "elpais",
              "lanacion", "infobae", "bbc", "cnn", "reuters")


def classify(url: str, head_meta: dict) -> str:
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    if head_meta.get("has_citation_meta") or any(h in host for h in SCI_DOMAIN_HINTS):
        return ContentCategory.SCIENTIFIC.value
    if any(h in host for h in GOV_TLD_HINTS):
        return ContentCategory.GOVERNMENT.value
    if any(h in host or h in path for h in NEWS_HINTS):
        return ContentCategory.NEWS.value
    if "blog" in host or "/blog" in path or "medium.com" in host:
        return ContentCategory.BLOG.value
    return ContentCategory.GENERIC.value


# ----------------------------------------------------------------------
# Extracción del cuerpo
# ----------------------------------------------------------------------
def _fallback_semantic_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for sel in NOISE_SELECTORS:
        for node in soup.select(sel):
            node.decompose()
    for sel in SEMANTIC_SELECTORS:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            return md(str(node), heading_style="ATX", strip=["img"]).strip()
    body = soup.body or soup
    return md(str(body), heading_style="ATX", strip=["img"]).strip()


def _reconcile_title(head_title: Optional[str], html: str) -> Optional[str]:
    """El <title>/og:title de muchos CMS es genérico ("Noticias - Sitio").

    Si el h1 de la página es sustantivo y no comparte ninguna palabra
    (>3 letras) con el título del head, el h1 es el titular real.
    """
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    h1_text = " ".join(h1.get_text(" ").split()) if h1 else ""
    if len(h1_text) < 15:
        return head_title
    if not head_title:
        return h1_text
    words = lambda s: {w.lower() for w in re.findall(r"\w{4,}", s)}
    if words(head_title) & words(h1_text):
        return head_title
    return h1_text


def extract_html_document(url: str, html: str, source_type: str,
                          min_words: int = 80) -> Optional[Document]:
    """HTML -> Document (markdown + metadata) o None si no hay contenido."""
    head_meta = scrape_head_metadata(html)
    head_meta["title"] = _reconcile_title(head_meta.get("title"), html)

    # 1) trafilatura: salida directamente en markdown
    body_md = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=True,
        include_comments=False,
        favor_recall=True,
    )
    traf_meta = trafilatura.extract_metadata(html)

    # 2) fallback semántico
    if not body_md or count_words(body_md) < min_words:
        candidate = _fallback_semantic_markdown(html)
        if count_words(candidate) > count_words(body_md or ""):
            body_md = candidate

    if not body_md or count_words(body_md) < min_words:
        log.info("Sin contenido sustantivo (%s palabras) en %s",
                 count_words(body_md or ''), url)
        return None

    canonical = head_meta.get("canonical") or url
    doc_id = make_doc_id(canonical)

    rights_info = {k: head_meta[k] for k in ("rights", "license_url")
                   if head_meta.get(k)}

    metadata = DocumentMetadata(
        doc_id=doc_id,
        url=url,
        canonical_url=canonical,
        title=head_meta.get("title") or (traf_meta.title if traf_meta else None),
        authors=head_meta.get("authors") or ([traf_meta.author] if traf_meta and traf_meta.author else []),
        published_date=head_meta.get("published_date") or (traf_meta.date if traf_meta else None),
        language=head_meta.get("language"),
        description=head_meta.get("description"),
        keywords=head_meta.get("keywords") or [],
        site_name=head_meta.get("site_name"),
        domain=urlparse(url).netloc,
        category=classify(url, head_meta),
        source_type=source_type,
        word_count=count_words(body_md),
    )

    if rights_info:
        metadata.extra["rights"] = rights_info

    # Encabezado YAML-like dentro del propio .md para trazabilidad
    front = (
        f"---\n"
        f"doc_id: {doc_id}\n"
        f"title: {metadata.title or 'Sin título'}\n"
        f"url: {url}\n"
        f"date: {metadata.published_date or 'desconocida'}\n"
        f"category: {metadata.category}\n"
        f"---\n\n"
    )
    return Document(metadata=metadata, markdown=front + body_md.strip() + "\n")
