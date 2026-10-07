"""Planificador de crawl por sitio: sondear, decidir, registrar.

Es la pieza "agéntica" del pipeline: con `discovery.mode: auto` el operador
ya no elige la estrategia — el sistema sondea la semilla una vez y decide
cómo atacar ese sitio, dejando el plan y sus razones registrados en
`analysis/<seed>-plan.pagemap.json` para auditoría.

Decisión heurística, en orden:

    la semilla es un LISTADO      -> "listing"   crawl clásico: los ítems y la
                                                  paginación alimentan la cola
    la semilla es un ARTÍCULO     -> "directed"  documento + adjuntos, sin
                                                  vagar por el nav del sitio
    portada con >=3 secciones     -> "sections"  árbol de secciones con
       de contenido en su nav                     presupuesto por rama
    portada opaca con sitemap     -> "sitemap"   la vía que el propio sitio
       poblado                                    ofrece (lección DSpace)
    cualquier otra cosa           -> "bfs"       exploración acotada

Árbitro LLM (opcional, `llm.py`): la heurística marca su decisión como
`low_confidence` cuando las señales son ambiguas (portada sin listado ni
artículo claros, con pocas secciones y sin sitemap). SOLO en esos casos, y
solo si hay un servidor Qwen disponible, se consulta al modelo para
desempatar. Con una sola GPU esto mantiene el costo acotado: el LLM se
invoca en el puñado de portadas dudosas, no en cada página.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("webharvest.planner")

VALID_STRATEGIES = ("listing", "directed", "sections", "sitemap", "bfs")


@dataclass
class CrawlPlan:
    strategy: str                       # listing | directed | sections | sitemap | bfs
    reasons: list[str] = field(default_factory=list)
    overrides: dict = field(default_factory=dict)
    signals: dict = field(default_factory=dict)
    low_confidence: bool = False        # la heurística no está segura
    decided_by: str = "heuristic"       # heuristic | llm

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "reasons": self.reasons,
                "overrides": self.overrides, "signals": self.signals,
                "low_confidence": self.low_confidence,
                "decided_by": self.decided_by}


_OVERRIDES = {
    "listing":  {"use_sitemaps": False, "mode": None},
    "directed": {"use_sitemaps": False, "mode": None, "follow_article_links": False},
    "sections": {"mode": "sections"},
    "sitemap":  {"use_sitemaps": True, "mode": None},
    "bfs":      {"use_sitemaps": False, "mode": None},
}


def decide_plan(page_kind: str, listing_items: int, sections_found: int,
                sitemap_urls: int, fulltext_links: int = 0) -> CrawlPlan:
    """Decisión heurística pura a partir de las señales del sondeo."""
    signals = {"page_kind": page_kind, "listing_items": listing_items,
               "sections_found": sections_found, "sitemap_urls": sitemap_urls,
               "fulltext_links": fulltext_links}

    if page_kind == "listing" and listing_items >= 3:
        return CrawlPlan("listing",
            [f"la semilla es un listado con {listing_items} ítems: los "
             "detalles y la paginación alimentan la cola"],
            dict(_OVERRIDES["listing"]), signals)

    if page_kind == "article":
        return CrawlPlan("directed",
            ["la semilla es un artículo: se extrae y se siguen solo sus "
             "adjuntos y texto completo, sin vagar por el nav"],
            dict(_OVERRIDES["directed"]), signals)

    if sections_found >= 3:
        return CrawlPlan("sections",
            [f"portada con {sections_found} secciones de contenido en su "
             "navegación: árbol de secciones con presupuesto por rama"],
            dict(_OVERRIDES["sections"]), signals)

    if sitemap_urls >= 10:
        return CrawlPlan("sitemap",
            [f"portada sin secciones útiles pero con sitemap de "
             f"{sitemap_urls}+ URLs: se usa la vía que el sitio ofrece"],
            dict(_OVERRIDES["sitemap"]), signals)

    # zona dudosa: navegación con 1-2 secciones y sin sitemap, o listado débil.
    # La heurística elige bfs pero se marca para que el árbitro LLM desempate.
    return CrawlPlan("bfs",
        ["señales ambiguas (ni listado ni artículo claros, pocas secciones, "
         "sin sitemap): exploración acotada, revisable por LLM"],
        dict(_OVERRIDES["bfs"]), signals, low_confidence=True)


# ----------------------------------------------------------------------
# Árbitro LLM (opcional)
# ----------------------------------------------------------------------
_SYSTEM = (
    "Eres un planificador de web scraping. Dada una descripción estructural "
    "de una página semilla, eliges la MEJOR estrategia de crawl para "
    "recolectar su texto. Estrategias:\n"
    "- listing: la página es una lista/índice de artículos con enlaces a "
    "detalles (se siguen ítems y paginación).\n"
    "- directed: la página ES un artículo/documento único (se extrae y se "
    "siguen solo sus adjuntos).\n"
    "- sections: es una portada con varias secciones temáticas navegables "
    "(se recorre cada sección).\n"
    "- sitemap: portada opaca; conviene usar el sitemap del sitio.\n"
    "- bfs: nada de lo anterior; exploración general de enlaces.\n"
    'Responde SOLO JSON: {"strategy": "...", "reason": "una frase breve"}.')


def _page_summary(title: str, headings: list[str], link_labels: list[str],
                  signals: dict) -> str:
    return (
        f"URL título: {title or '(sin título)'}\n"
        f"Clasificación estructural previa: {signals.get('page_kind')}\n"
        f"Ítems de listado detectados: {signals.get('listing_items')}\n"
        f"Secciones de navegación detectadas: {signals.get('sections_found')}\n"
        f"URLs en sitemap: {signals.get('sitemap_urls')}\n"
        f"Encabezados de la página: {headings[:12]}\n"
        f"Muestra de textos de enlaces: {link_labels[:20]}")


def refine_with_llm(plan: CrawlPlan, llm, title: str, headings: list[str],
                    link_labels: list[str]) -> CrawlPlan:
    """Si el plan es dudoso y hay LLM, deja que el modelo lo revise.

    Devuelve el plan original si el LLM no está, falla, o propone algo
    inválido — la heurística siempre es el piso.
    """
    if not plan.low_confidence or llm is None:
        return plan
    answer = llm.ask_json(_SYSTEM,
                          _page_summary(title, headings, link_labels, plan.signals))
    if not answer:
        return plan
    strat = str(answer.get("strategy", "")).strip().lower()
    if strat not in VALID_STRATEGIES:
        log.info("LLM propuso estrategia inválida %r; se mantiene heurística", strat)
        return plan
    reason = str(answer.get("reason", "")).strip()[:200] or "(sin motivo)"
    log.info("Árbitro LLM: %s -> %s (%s)", plan.strategy, strat, reason)
    return CrawlPlan(strat, [f"árbitro LLM: {reason}"], dict(_OVERRIDES[strat]),
                     plan.signals, low_confidence=False, decided_by="llm")
