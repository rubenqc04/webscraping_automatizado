"""Orquestador: une descubrimiento, cumplimiento, fetching, extracción
y almacenamiento.

Flujo por semilla:

    seed ──► sitemaps + BFS de enlaces ──► Frontier (cola priorizada)
                                              │
                       ┌──────────────────────┘
                       ▼
              robots.txt ok? ──no──► log & skip
                       │sí
                       ▼
              throttle por dominio (crawl-delay)
                       ▼
              AdaptiveFetcher (httpx ─► Playwright si SPA)
                       │
        ┌──────────────┼───────────────────┐
        ▼              ▼                   ▼
     bloqueado      es PDF             es HTML
   (captcha/403)      │                   │
   log & marcar    pdf_extractor      html_extractor
    dominio        (pymupdf4llm       (trafilatura +
                    + triaje OCR)      fallbacks)
        │              │                   │
        └──────────────┴───────┬───────────┘
                               ▼
                        DocumentStore
              (md + pdf + json + índice + cola OCR)
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse, unquote

from .analysis.page_map import PageMap, build_page_map, synthesize_next_page_urls
from .compliance.policy import DomainThrottle, RobotsPolicy
from .discovery.crawler import (CrawlTarget, Frontier, bare_host,
                                discover_sitemap_urls, extract_links,
                                looks_like_content, same_site)
from .discovery.sections import discover_sections
from .extractors.docx_extractor import extract_docx_document
from .extractors.html_extractor import extract_html_document
from .extractors.pdf_extractor import extract_pdf_document
from .fetchers.fetchers import AdaptiveFetcher
from .models import SourceType, make_doc_id
from .planner import decide_plan, refine_with_llm
from .storage.store import DocumentStore

log = logging.getLogger("webharvest.pipeline")


_GENERIC_FILE_WORDS = {
    "archivo", "archivos", "articles", "article", "file", "files", "doc",
    "docs", "documento", "documentos", "adjunto", "anexo", "attachment",
    "download", "descarga", "final", "copia", "version", "definitivo",
}
_DOC_EXT_RE = re.compile(r"\.(pdf|docx?|odt|rtf)$", re.I)


def _title_from_filename(url: str) -> str | None:
    """Título legible a partir del nombre de archivo de la URL."""
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    name = _DOC_EXT_RE.sub("", name)
    name = re.sub(r"[_+]+", " ", name)
    name = re.sub(r"\s*\(\d+\)\s*$", "", name)          # "(1)", "(definitivo)" no
    name = re.sub(r"[-\s]{2,}", " ", name).strip(" -.")
    if len(name) < 4:
        return None
    # Nombres genéricos de CMS ("articles-90118_archivo_01", "file_2.pdf") no
    # describen nada: mejor dejar que decida el fallback por seed_context.
    words = [w for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{4,}", name)
             if w.lower() not in _GENERIC_FILE_WORDS]
    if not words:
        return None
    if name.isupper() and len(name) > 12:                  # "GUIA EDUCATIVA" -> Guia Educativa
        name = name.title()
    return name[:180]


def _page_digest(html: str) -> tuple[str, list[str], list[str]]:
    """Título, encabezados y muestra de textos de enlace, para el árbitro LLM."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(strip=True) if soup.title else "")[:120]
    headings = [" ".join(h.get_text(" ").split())[:80]
                for h in soup.find_all(["h1", "h2", "h3"])][:12]
    labels, seen = [], set()
    for a in soup.find_all("a", href=True):
        t = " ".join(a.get_text(" ").split())
        if 2 <= len(t) <= 40 and t.lower() not in seen:
            seen.add(t.lower()); labels.append(t)
        if len(labels) >= 20:
            break
    return title, headings, labels


class ScrapePipeline:
    """Un pipeline = un hilo de crawl.

    En modo paralelo (ver `parallel.ParallelHarvester`) se crea un pipeline
    por worker y se le inyectan los componentes COMPARTIDOS (store, throttle,
    robots — todos con lock interno), mientras el fetcher y el estado de
    crawl quedan por worker: el navegador de Playwright y `self._prov` /
    `self._listing_pages` no son thread-safe.
    """

    def __init__(self, cfg: dict, *, store: DocumentStore | None = None,
                 throttle: DomainThrottle | None = None,
                 robots: RobotsPolicy | None = None):
        self.cfg = cfg
        self.robots = robots or RobotsPolicy(
            user_agent=cfg["identity"]["user_agent"],
            respect=cfg["compliance"]["respect_robots_txt"],
            default_delay=cfg["compliance"]["default_crawl_delay"],
        )
        self.throttle = throttle or DomainThrottle(
            self.robots, cfg["compliance"]["max_requests_per_domain"]
        )
        self.fetcher = AdaptiveFetcher(cfg)
        self.store = store or DocumentStore(cfg["storage"])
        self.min_words = cfg["extraction"]["min_words_for_valid_article"]
        self.pdf_cfg = cfg["extraction"]["pdf"]
        self.analysis_cfg = cfg.get("analysis", {})
        # Tope de páginas de listado por semilla (paginación incluida):
        # evita que la paginación sintetizada consuma todo el presupuesto.
        self.max_listing_pages = cfg["discovery"].get("max_listing_pages", 3)
        # Modo exhaustivo: sin topes arbitrarios de profundidad ni paginación.
        # Los rieles pasan a ser la SATURACIÓN (abajo) y max_pages_per_site.
        if cfg["discovery"].get("exhaustive"):
            self.max_listing_pages = 10 ** 6
        self._listing_pages = 0
        # Saturación: si en las últimas `saturation_window` páginas obtenidas
        # no salió ni un documento nuevo, el sitio dejó de rendir y se cierra
        # solo — sustituye a los topes fijos y evita que un dominio de bajo
        # rendimiento monopolice un worker (el hallazgo del "rezagado").
        self.saturation_window = cfg["discovery"].get("saturation_window", 25)
        # Presupuesto de TIEMPO por sitio. El de páginas no acota el reloj: en
        # un sitio documental el tiempo lo consume descargar y extraer PDFs, no
        # navegar. Medido: un museo rindió 195 documentos/hora y un sitio con
        # PDFs de 175.000 palabras, 34 — seis veces más lento con el mismo tope
        # de páginas, y cortado por el límite del job a mitad de camino. Con
        # esta perilla, una corrida de N sitios dura lo que uno planificó.
        self.max_seconds_per_site = cfg["discovery"].get("max_seconds_per_site")
        # El cupo de cortesía por dominio acota TODO el crawl del sitio: si es
        # menor que el presupuesto de páginas, las últimas secciones quedan
        # sin visitar por "throttle" aunque haya presupuesto (benchmark de
        # cobertura: 400 requests vs 900 páginas -> Noticias 0 documentos).
        quota = cfg["compliance"]["max_requests_per_domain"]
        budget = cfg["discovery"]["max_pages_per_site"]
        if quota < budget:
            log.warning("compliance.max_requests_per_domain=%d < discovery."
                        "max_pages_per_site=%d: el cupo del dominio cortará "
                        "el crawl antes que el presupuesto", quota, budget)
        self._site_fetches = 0
        self._last_yield_fetch = 0
        self._site_started = time.monotonic()
        # Documentos guardados por ESTE pipeline (no por el store, que en
        # modo paralelo lo comparten todos los workers): los conteos por
        # sección salían mezclados con lo que guardaban los otros dominios
        # (hallazgo del benchmark de cobertura: 'Noticias: 0 documentos' en
        # un sitio que sí había guardado 12).
        self._docs_saved = 0
        # Árbitro LLM opcional para el planner (solo en portadas ambiguas)
        self.llm = None
        llm_cfg = cfg["discovery"].get("llm") or {}
        if llm_cfg.get("enabled"):
            from .llm import LLMClient
            self.llm = LLMClient(
                endpoint=llm_cfg.get("endpoint", "http://localhost:8811"),
                model=llm_cfg.get("model", "qwen2.5-32b-instruct"),
                timeout=llm_cfg.get("timeout", 20.0))
        # Grafo de procedencia del crawl: url -> {via, kind, depth, section?}.
        # Permite reconstruir para cada documento el camino completo desde
        # la semilla (crawl_path) y armar el árbol del sitio para auditoría.
        self._prov: dict[str, dict] = {}
        # Estado del modo secciones recursivo (ver _crawl_section_tree)
        self._section_root: str | None = None
        self._found_subsections: list = []
        self._seed_context: dict = {}
        self._current_ctx: dict | None = None
        self._cur_seed: str | None = None
        # otros hosts del sitio en curso (sin www.), p.ej. adonde redirige la
        # semilla; las frontiers del sitio lo comparten por referencia
        self._site_hosts: set[str] = set()

    # ------------------------------------------------------------------
    def _track(self, frontier: Frontier, target: CrawlTarget, seed: str,
               via: str | None, kind: str, section: str | None = None) -> bool:
        """frontier.add + registro de procedencia (solo si la URL es nueva)."""
        if not frontier.add(target, seed):
            return False
        self._note(target.url, "enqueued", seed=seed, via=via, kind=kind)
        node = {"via": via, "kind": kind, "depth": target.depth}
        if section:
            node["section"] = section
        self._prov[target.url] = node
        return True

    def _path(self, url: str, max_hops: int = 12) -> list[dict]:
        """Cadena semilla -> ... -> url, siguiendo el grafo de procedencia."""
        path, cur, seen = [], url, set()
        while cur and cur not in seen and len(path) < max_hops:
            seen.add(cur)
            node = self._prov.get(cur)
            if node is None:
                break
            step = {"url": cur, "kind": node["kind"]}
            if node.get("section"):
                step["section"] = node["section"]
            path.append(step)
            cur = node.get("via")
        return list(reversed(path))

    def _stamp(self, doc, target: CrawlTarget) -> None:
        doc.metadata.depth = target.depth
        doc.metadata.crawl_path = self._path(target.url)
        # Binarios sin título propio: el NOMBRE DEL ARCHIVO describe el
        # documento ("GUIA EDUCATIVA DESCARGABLE.pdf") mucho mejor que el
        # nombre de la institución, que era el fallback por seed_context y
        # dejaba 40 PDFs/Word titulados todos igual.
        if not (doc.metadata.title or "").strip():
            name = _title_from_filename(doc.metadata.url)
            if name:
                doc.metadata.title = name
                doc.metadata.extra["title_from"] = "filename"

        if self._current_ctx:
            # p.ej. la consulta de búsqueda dirigida que originó esta semilla
            doc.metadata.extra["seed_context"] = self._current_ctx
            # Muchos PDFs no traen título nativo: el rótulo con que la fuente
            # (Excel de autorizadas, CSV de search_links) nombró el enlace es
            # mejor que dejarlo vacío.
            if not (doc.metadata.title or "").strip():
                for key in ("Titulo", "Título", "titulo", "title",
                            "Institucion", "Institución"):
                    label = (self._current_ctx.get(key) or "").strip()
                    if len(label) >= 8:
                        doc.metadata.title = label
                        doc.metadata.extra["title_from"] = f"seed_context.{key}"
                        break

    def _save_doc(self, doc, seed: str | None = None) -> None:
        """Guardado + señal de rendimiento para la saturación."""
        if not self.store.save(doc):
            # mismo texto que un documento ya guardado (p.ej. la versión
            # ?current=pdf de la misma página): no rinde ni cuenta
            self._note(doc.metadata.url, "duplicate", seed=seed)
            return
        self._last_yield_fetch = self._site_fetches
        self._docs_saved += 1
        self._note(doc.metadata.url, "saved", seed=seed)

    def _note(self, url: str, decision: str, *, seed: str | None = None,
              reason: str | None = None, via: str | None = None,
              kind: str | None = None) -> None:
        """Anota en el libro de visitas (ver storage/ledger.py)."""
        if self.store.ledger is not None:
            self.store.ledger.record(url, decision, seed=seed or self._cur_seed,
                                     reason=reason, via=via, kind=kind)

    def _new_frontier(self, cfg_discovery: dict, seed: str,
                      seen: set | None = None) -> Frontier:
        return Frontier(cfg_discovery, seen=seen, site_hosts=self._site_hosts,
                        on_reject=lambda url, why: self._note(
                            url, "rejected", seed=seed, reason=why))

    def _adopt_redirect(self, seed: str, final_url: str) -> None:
        """Si la portada redirige a otro host, ese host es el sitio.

        junji.gob.cl redirige a junji.cl y masmujeres.serviciocivil.cl a
        redmujeres.serviciocivil.cl: con el alcance atado al host de la
        semilla, todos los enlaces de la portada se rechazaban como
        "offsite" y el sitio cerraba con 0 documentos. Solo la redirección
        de la portada misma amplía el alcance, no la de cualquier enlace.
        """
        host = bare_host(final_url)
        if host and host != bare_host(seed) and host not in self._site_hosts:
            self._site_hosts.add(host)
            log.info("La semilla %s redirige a %s: se incluye en el alcance del sitio",
                     seed, host)

    def _seed_closed(self, seed: str, decision: str, reason: str | None = None) -> None:
        """La portada misma no se pudo usar: dejarlo en el libro y en la
        procedencia, para que un sitio con 0 documentos diga por qué."""
        self._note(seed, decision, seed=seed, reason=reason)
        why = f"portada: {decision}" + (f" ({reason})" if reason else "")
        log.info("Sitio %s cerrado: %s", seed, why)
        self.store.save_analysis(f"{make_doc_id(seed)}-provenance",
                                 {"seed": seed, "terminated": why,
                                  "nodes": {seed: {"via": None, "kind": "seed",
                                                   "depth": 0}}})

    def _close_frontier(self, frontier: Frontier, seed: str, why: str) -> None:
        for t in frontier.drain():
            self._note(t.url, "not_fetched", seed=seed, reason=why)

    def _reset_site_counters(self) -> None:
        """Reinicia los contadores de SATURACIÓN.

        Se llama al empezar cada sección, porque la saturación se mide por
        rama: una sección que deja de rendir no debe arrastrar a las demás.
        El reloj del sitio NO se toca aquí — ver `_start_site_clock`.
        """
        self._site_fetches = 0
        self._last_yield_fetch = 0

    def _start_site_clock(self) -> None:
        """Arranca el presupuesto de tiempo, UNA vez por sitio.

        Estaba dentro de `_reset_site_counters`, y como eso corre al empezar
        cada sección, el "presupuesto por sitio" era en realidad por sección:
        un sitio con 2 secciones y 5 h de presupuesto corría 10 h y volvía a
        chocar con el límite del job, que es justo lo que la perilla venía a
        evitar.
        """
        self._site_started = time.monotonic()

    def _saturated(self) -> str | None:
        """Razón para cerrar el sitio, o None para seguir.

        Dos rieles independientes: dejar de rendir documentos (saturación) y
        agotar el tiempo asignado al sitio.
        """
        idle = self._site_fetches - self._last_yield_fetch
        if idle >= self.saturation_window:
            return (f"{idle} páginas seguidas sin documentos nuevos "
                    f"(ventana {self.saturation_window})")
        if self.max_seconds_per_site:
            spent = time.monotonic() - self._site_started
            if spent >= self.max_seconds_per_site:
                return (f"presupuesto de tiempo agotado "
                        f"({spent/60:.0f} min de {self.max_seconds_per_site/60:.0f})")
        return None

    # ------------------------------------------------------------------
    def run(self, seeds: list[str],
            seed_context: dict[str, dict] | None = None) -> dict:
        """`seed_context[url]` (opcional) se adjunta como extra.seed_context a
        todo documento recolectado desde esa semilla — p.ej. la consulta de
        búsqueda dirigida (search_links) que la produjo."""
        self._seed_context = seed_context or {}
        for seed in seeds:
            self._current_ctx = self._seed_context.get(seed)
            self._cur_seed = seed
            try:
                self._crawl_site(seed)
            except Exception:
                log.exception("Fallo no recuperable en semilla %s", seed)
        stats = self.store.stats()
        stats["blocked_domains"] = self.throttle.blocked_domains()
        return stats

    # ------------------------------------------------------------------
    def _crawl_site(self, seed: str) -> None:
        self._start_site_clock()
        self._site_hosts.clear()
        mode = self.cfg["discovery"].get("mode")
        if mode == "sections":
            self._crawl_by_sections(seed)
            return
        if mode == "auto":
            self._crawl_auto(seed)
            return
        self._crawl_classic(seed)

    # ------------------------------------------------------------------
    def _crawl_classic(self, seed: str) -> None:
        log.info("=== Semilla: %s ===", seed)
        self._listing_pages = 0
        self._reset_site_counters()
        self._prov = {}
        frontier = self._new_frontier(self.cfg["discovery"], seed)
        self._track(frontier, CrawlTarget(seed, depth=0), seed, None, "seed")

        if self.cfg["discovery"]["use_sitemaps"]:
            # Ingesta acotada al presupuesto real: un sitemap gigante (50k+
            # URLs en DSpace y similares) inundaría la deduplicación y las
            # colas, ahogando lo que el crawl descubre en las propias páginas.
            limit = min(self.cfg["discovery"].get("sitemap_limit", 500),
                        self.cfg["discovery"]["max_pages_per_site"] * 3)
            found = discover_sitemap_urls(seed, self.cfg["identity"]["user_agent"],
                                          limit=max(limit * 20, 2000))
            # dentro de la ingesta, primero lo que parece contenido
            hints = self.cfg["discovery"]["content_hints"]
            found.sort(key=lambda u: not looks_like_content(u, hints))
            for url in found[:limit]:
                hint_pdf = url.lower().split("?")[0].endswith(".pdf")
                self._track(frontier, CrawlTarget(url, depth=1, hint_pdf=hint_pdf,
                                                  from_sitemap=True),
                            seed, seed, "sitemap")

        terminated = "frontera agotada"
        while (target := frontier.next()) is not None:
            self._process(target, seed, frontier)
            if (reason := self._saturated()):
                terminated = f"saturación: {reason}"
                log.info("Sitio %s cerrado por %s", seed, terminated)
                break
        else:
            if frontier._emitted >= frontier.max_pages:
                terminated = "presupuesto de páginas agotado"
        self._close_frontier(frontier, seed, terminated)
        self.store.save_analysis(f"{make_doc_id(seed)}-provenance",
                                 {"seed": seed, "terminated": terminated,
                                  "fetches": self._site_fetches,
                                  "nodes": self._prov})

    # ------------------------------------------------------------------
    def _fetch_seed(self, seed: str):
        """Obtiene la portada para los modos que la analizan antes de
        recorrer (auto, secciones). Si no sirve, registra el motivo con
        `_seed_closed` y devuelve None. Antes estos cierres no dejaban
        rastro: el panel mostraba "terminado" con 0 y sin explicación."""
        if not self.robots.can_fetch(seed):
            self._seed_closed(seed, "robots", "robots.txt prohíbe la portada")
            return None
        if not self.throttle.wait_turn(seed):
            self._seed_closed(seed, "throttle")
            return None
        result = self.fetcher.fetch(seed)
        if result.block.is_blocked:
            self._seed_closed(seed, "blocked", result.block.detail)
            return None
        if (result.status_code or 0) >= 400:
            self._seed_closed(seed, "http_error", f"http {result.status_code}")
            return None
        self._adopt_redirect(seed, result.final_url)
        return result

    # ------------------------------------------------------------------
    def _crawl_auto(self, seed: str) -> None:
        """Modo agéntico: sondear la semilla, decidir la estrategia, ejecutar.

        Un solo fetch de sondeo alimenta la decisión (ver `planner.py`); el
        plan elegido y sus razones quedan en analysis/<seed>-plan.pagemap.json.
        La ejecución reutiliza las estrategias existentes con overrides sobre
        `discovery`, restaurados al terminar (seguro: un pipeline = un hilo).
        """
        log.info("=== Semilla (modo auto): %s ===", seed)
        result = self._fetch_seed(seed)
        if result is None:
            return
        self._site_fetches += 1

        # binario directo (PDF/Word como semilla): no hay nada que planificar
        if result.is_pdf or result.is_word or not result.html:
            plan = decide_plan("binary", 0, 0, 0)
            plan.strategy, plan.reasons = "directed", [
                "la semilla es un binario o no tiene HTML: procesamiento directo"]
        else:
            pmap = build_page_map(result.final_url, result.html,
                                  self.analysis_cfg)
            self.store.save_analysis(make_doc_id(seed),
                                     {"fetch_method": result.method,
                                      **pmap.to_dict()})
            sections = []
            if pmap.page_kind not in ("listing", "article"):
                sections = discover_sections(
                    result.final_url, result.html,
                    self.cfg["discovery"].get("max_sections", 8))
            sitemap_urls = 0
            if (pmap.page_kind not in ("listing", "article")
                    and len(sections) < 3):
                sitemap_urls = len(discover_sitemap_urls(
                    seed, self.cfg["identity"]["user_agent"], limit=50))
            plan = decide_plan(pmap.page_kind, len(pmap.listing_items),
                               len(sections), sitemap_urls,
                               len(pmap.fulltext_links))
            # Desempate por LLM SOLO si la heurística quedó dudosa y hay servidor
            if plan.low_confidence and self.llm is not None:
                title, headings, labels = _page_digest(result.html)
                plan = refine_with_llm(plan, self.llm, title, headings, labels)

        log.info("Plan para %s: %s [%s] — %s", seed, plan.strategy,
                 plan.decided_by, "; ".join(plan.reasons))
        self.store.save_analysis(f"{make_doc_id(seed)}-plan", plan.to_dict())

        # ejecutar con overrides temporales (un pipeline = un hilo)
        old_cfg, old_mlp = self.cfg, self.max_listing_pages
        overrides = {k: v for k, v in plan.overrides.items() if k != "mode"}
        self.cfg = {**old_cfg,
                    "discovery": {**old_cfg["discovery"], **overrides}}
        try:
            if plan.strategy == "sections":
                self._crawl_by_sections(seed, prefetched=(result, pmap))
            else:
                # la semilla se re-encola; el fetch de sondeo costó 1 request
                # extra que el throttle ya espació
                self._crawl_classic(seed)
        finally:
            self.cfg, self.max_listing_pages = old_cfg, old_mlp

    # ------------------------------------------------------------------
    def _crawl_by_sections(self, seed: str, prefetched=None) -> None:
        """Modo cognitivo por secciones: home -> secciones -> profundizar.

        La portada se analiza una vez para descubrir las secciones de
        contenido (nav/menú); cada sección se crawlea después como un
        sub-árbol con su propio presupuesto (`max_pages_per_section`),
        de modo que una sección enorme no ahogue a las demás. El mapa
        de secciones y las estadísticas por sección quedan en
        analysis/<seed_id>-sections.pagemap.json.
        """
        log.info("=== Semilla (modo secciones): %s ===", seed)
        self._prov = {seed: {"via": None, "kind": "seed", "depth": 0}}
        if prefetched is not None:
            result, pmap = prefetched          # el planner ya sondeó la portada
        else:
            result = self._fetch_seed(seed)
            if result is None:
                return
            if not result.html:
                self._seed_closed(seed, "empty", f"http {result.status_code}")
                return
            pmap = build_page_map(result.final_url, result.html, self.analysis_cfg)
            self.store.save_analysis(make_doc_id(seed),
                                     {"fetch_method": result.method, **pmap.to_dict()})

        max_sections = self.cfg["discovery"].get("max_sections", 8)
        sections = discover_sections(result.final_url, result.html, max_sections)
        log.info("Portada %s: %d secciones descubiertas: %s",
                 result.final_url, len(sections),
                 ", ".join(s.name for s in sections))
        if not sections:
            # Sin menú reconocible (portadas de plataformas, sitios de una
            # página, menús armados por JS) el modo secciones no recorría
            # NADA y el sitio cerraba con 0: funciona.serviciocivil.cl,
            # geoportal.cl, ingles.mineduc.cl... Mejor el recorrido clásico
            # desde la portada, con el presupuesto del sitio.
            log.info("Sin secciones en %s: recorrido clásico desde la portada", seed)
            self._crawl_classic(seed)
            return

        per_budget = self.cfg["discovery"].get("max_pages_per_section", 8)
        # Profundidad del árbol de secciones: 1 = solo secciones de la portada
        # (comportamiento clásico); 2 = también sub-secciones (Regiones ->
        # Arica, Tarapacá...), cada nivel con la mitad del presupuesto.
        max_sec_depth = self.cfg["discovery"].get("max_section_depth", 1)
        report = []
        shared_seen: set = {seed}
        for sec in sections:
            self._crawl_section_tree(sec, seed, per_budget,
                                     max_sec_depth - 1, shared_seen, report)

        self.store.save_analysis(f"{make_doc_id(seed)}-provenance",
                                 {"seed": seed,
                                  "terminated": "árbol de secciones completado",
                                  "nodes": self._prov})
        self.store.save_analysis(f"{make_doc_id(seed)}-sections", {
            "seed": seed, "mode": "sections",
            "sections_discovered": len(sections),
            "per_section_budget": per_budget,
            "sections": report,
        })

    # ------------------------------------------------------------------
    def _crawl_section_tree(self, sec, seed: str, budget: int, depth_left: int,
                            shared_seen: set, report: list,
                            parent_name: str = "") -> None:
        """Crawlea una sección y, si quedan niveles, sus sub-secciones.

        Las sub-secciones se descubren en la nav de la página de la sección
        y se reconocen porque su ruta EXTIENDE la de la sección padre
        (/noticias/regiones -> /noticias/regiones/region-de-arica). Cada
        nivel recibe la mitad del presupuesto del padre (mínimo 2 páginas).
        """
        label = f"{parent_name} > {sec.name}" if parent_name else sec.name
        self._listing_pages = 0
        self._section_root = sec.url if depth_left > 0 else None
        self._found_subsections = []
        docs_before = self._docs_saved

        sub_cfg = {**self.cfg["discovery"], "max_pages_per_site": budget}
        # La raíz de la sección siempre entra a su propia frontier, aunque
        # una sección anterior la haya visto como enlace: el `seen` compartido
        # dedupe páginas internas, no debe robarse las raíces de sección.
        shared_seen.discard(sec.url)
        frontier = self._new_frontier(sub_cfg, seed, seen=shared_seen)
        self._track(frontier, CrawlTarget(sec.url, depth=0), seed,
                    seed, "section", section=label)
        self._reset_site_counters()
        while (target := frontier.next()) is not None:
            self._process(target, seed, frontier)
            if (reason := self._saturated()):
                log.info("Sección '%s' cerrada por saturación: %s", label, reason)
                self._close_frontier(frontier, seed, f"saturación: {reason}")
                break
        else:
            self._close_frontier(frontier, seed,
                                 "presupuesto de sección agotado"
                                 if frontier._emitted >= frontier.max_pages
                                 else "frontera agotada")

        docs_after = self._docs_saved
        report.append({"section": label, "url": sec.url, "source": sec.source,
                       "budget": budget,
                       "documents_collected": docs_after - docs_before})
        log.info("Sección '%s': %d documentos", label, docs_after - docs_before)

        if depth_left > 0 and self._found_subsections:
            max_subs = self.cfg["discovery"].get("max_subsections", 4)
            subs = self._found_subsections[:max_subs]
            sub_budget = max(2, budget // 2)
            log.info("Sección '%s': %d sub-secciones -> %s (presupuesto %d c/u)",
                     label, len(subs), ", ".join(s.name for s in subs), sub_budget)
            for sub in subs:
                self._crawl_section_tree(sub, seed, sub_budget, depth_left - 1,
                                         shared_seen, report, parent_name=label)

    # ------------------------------------------------------------------
    def _process(self, target: CrawlTarget, seed: str, frontier: Frontier) -> None:
        url = target.url

        if self.store.already_saved(make_doc_id(url)):
            self._note(url, "already_saved", seed=seed)
            return
        if not self.robots.can_fetch(url):
            self._note(url, "robots", seed=seed)
            return
        if not self.throttle.wait_turn(url):
            self._note(url, "throttle", seed=seed)
            return

        result = self.fetcher.fetch(url)
        self._site_fetches += 1
        if url == seed:
            self._adopt_redirect(seed, result.final_url)

        # --- Bloqueos: registrar y respetar --------------------------------
        if result.block.is_blocked:
            if result.block.kind == "captcha":
                # Un captcha es el sitio diciendo "no a bots": se respeta.
                self.throttle.mark_blocked(url, f"captcha detectado ({result.block.detail})")
            elif result.block.kind == "http_block" and result.status_code in (403, 429):
                self.throttle.mark_blocked(url, result.block.detail)
            else:
                log.info("Omitida %s: %s", url, result.block.detail)
            self._note(url, "blocked", seed=seed, reason=result.block.detail)
            return

        # --- Respuestas de error: la página de error NO es un documento ----
        # Los 401/403/407/429/503 ya los trató BlockDetector como bloqueo del
        # dominio. El resto de 4xx/5xx llega con cuerpo HTML útil-en-apariencia
        # ("Página no encontrada", "521: Web server is down", "Email
        # Protection | Cloudflare") y se extraía como si fuera contenido: 24
        # documentos basura repartidos por los corpus. Un enlace roto no es
        # un hallazgo, es un enlace roto.
        if (result.status_code or 0) >= 400:
            self._note(url, "http_error", seed=seed,
                       reason=f"http {result.status_code}")
            return

        # --- PDF ------------------------------------------------------------
        if result.is_pdf and self.pdf_cfg["download"]:
            size_mb = len(result.body_bytes or b"") / 1_048_576
            if size_mb > self.pdf_cfg["max_size_mb"]:
                log.info("PDF demasiado grande (%.1f MB): %s", size_mb, url)
                self._note(url, "too_large", seed=seed, reason=f"{size_mb:.1f} MB")
                return
            try:
                doc = extract_pdf_document(
                    url, result.body_bytes,
                    chars_threshold=self.pdf_cfg["ocr_chars_per_page_threshold"],
                    image_ratio_threshold=self.pdf_cfg["ocr_image_coverage_threshold"],
                )
                doc.metadata.fetch_method = "download"
                doc.metadata.http_status = result.status_code
                self._stamp(doc, target)
                self._save_doc(doc, seed)
            except Exception:
                log.exception("Error extrayendo PDF %s", url)
                self._note(url, "extract_error", seed=seed, reason="pdf")
            return

        # --- Word (.docx / .doc) ---------------------------------------------
        if result.is_word and self.pdf_cfg["download"]:
            size_mb = len(result.body_bytes or b"") / 1_048_576
            if size_mb > self.pdf_cfg["max_size_mb"]:
                log.info("Word demasiado grande (%.1f MB): %s", size_mb, url)
                self._note(url, "too_large", seed=seed, reason=f"{size_mb:.1f} MB")
                return
            try:
                doc = extract_docx_document(url, result.body_bytes)
                doc.metadata.fetch_method = "download"
                doc.metadata.http_status = result.status_code
                self._stamp(doc, target)
                self._save_doc(doc, seed)
            except Exception:
                log.exception("Error extrayendo Word %s", url)
                self._note(url, "extract_error", seed=seed, reason="word")
            return

        # --- HTML -----------------------------------------------------------
        if not result.html:
            self._note(url, "empty", seed=seed,
                       reason=f"http {result.status_code}")
            return

        # Un candidato a documento (hint_pdf) puede cruzar dominio SOLO como
        # binario; si resultó ser HTML (Wayback, captcha del publisher, visor)
        # queda fuera del alcance del crawl y no se extrae.
        if target.hint_pdf and not same_site(
                result.final_url, seed,
                self.cfg["discovery"]["follow_subdomains"], self._site_hosts):
            log.info("HTML fuera de dominio descartado (se esperaba documento): %s",
                     result.final_url)
            self._note(url, "offsite_html", seed=seed, reason=result.final_url)
            return

        # PDFs capturados por la sonda de clic (acciones JS sin href):
        # se procesan de inmediato, los bytes vienen de URLs firmadas que expiran.
        if result.discovered_pdfs and self.pdf_cfg["download"]:
            self._save_discovered_pdfs(result, target)

        # Modo secciones recursivo: en la raíz de una sección, mirar su nav
        # buscando sub-secciones (rutas que extienden la de la sección).
        if self._section_root and target.url == self._section_root:
            parent_path = urlparse(self._section_root).path.rstrip("/") + "/"
            cands = discover_sections(result.final_url, result.html,
                                      max_sections=24)
            self._found_subsections = [
                s for s in cands
                if urlparse(s.url).path.startswith(parent_path)
                and urlparse(s.url).path.rstrip("/") != parent_path.rstrip("/")]

        # Mapa semántico: secciones, concentración de texto y tipo de página
        # (listado / artículo / navegación). Guía la extracción y el crawl.
        pmap = build_page_map(result.final_url, result.html, self.analysis_cfg)
        self.store.save_analysis(make_doc_id(url),
                                 {"fetch_method": result.method, **pmap.to_dict()})

        # LISTADO: encolar ítems (detalle) y paginación; no es un "documento".
        if pmap.page_kind == "listing":
            self._note(url, "listing", seed=seed,
                       reason=f"{len(pmap.listing_items)} ítems")
            self._handle_listing(pmap, result.final_url, target, seed, frontier)
            if self.cfg["discovery"].get("exhaustive"):
                # Red de seguridad: si el extractor de ítems deja alguno fuera,
                # el BFS (prioridad baja) lo alcanza igual. El riel sigue
                # siendo la saturación.
                html_links, doc_links = extract_links(result.final_url, result.html)
                for link in doc_links:
                    self._track(frontier, CrawlTarget(link, depth=target.depth + 1,
                                                      hint_pdf=True),
                                seed, url, "document_link")
                for link in html_links:
                    self._track(frontier, CrawlTarget(link, depth=target.depth + 1),
                                seed, url, "bfs")
            return

        source_type = (SourceType.HTML_DYNAMIC.value if result.method == "playwright"
                       else SourceType.HTML_STATIC.value)
        try:
            doc = extract_html_document(result.final_url, result.html,
                                        source_type, self.min_words)
        except Exception:
            log.exception("Error extrayendo HTML %s", url)
            self._note(url, "extract_error", seed=seed, reason="html")
            doc = None

        # Portadillas: una página cuyo texto es casi puro enlace es un hub
        # de titulares, no un documento — y su "título" suele ser el titular
        # robado del teaser del momento (hallazgo de la auditoría en portadas
        # regionales de noticias). Calibrado con datos reales: portadillas
        # ld=0.96-0.97 vs stubs científicos legítimos ld<=0.69.
        max_ld = self.cfg["extraction"].get("max_link_density", 0.85)
        if doc and pmap.link_density >= max_ld:
            log.info("Portadilla descartada (densidad de enlaces %.2f >= %.2f, "
                     "%d palabras): %s", pmap.link_density, max_ld,
                     doc.metadata.word_count, result.final_url)
            self._note(url, "hub", seed=seed,
                       reason=f"link_density {pmap.link_density:.2f}")
            doc = None
        elif doc is None:
            self._note(url, "no_document", seed=seed,
                       reason=f"page_kind {pmap.page_kind}")

        if doc:
            doc.metadata.fetch_method = result.method
            doc.metadata.http_status = result.status_code
            doc.metadata.extra["page_kind"] = pmap.page_kind
            if pmap.fulltext_links:
                doc.metadata.extra["fulltext_links"] = pmap.fulltext_links
            self._stamp(doc, target)
            self._save_doc(doc, seed)

        # Ítems de grupos fuertes (layouts "revista": portada con titulares
        # además del contenido propio): profundizar siempre en ellos.
        for item in pmap.listing_items:
            self._track(frontier, CrawlTarget(item.url, depth=target.depth + 1,
                                              from_listing=True),
                        seed, url, "listing_item")

        # ...y su paginación: si esta página rindió ítems, las siguientes
        # rendirán más. Se exige que HAYA ítems para no seguir números
        # sueltos de un artículo (notas al pie, tablas).
        if pmap.listing_items and pmap.pagination_urls:
            for pag_url in pmap.pagination_urls[:self.max_listing_pages]:
                self._track(frontier, CrawlTarget(pag_url, depth=target.depth,
                                                  from_listing=True),
                            seed, url, "pagination")

        # ARTÍCULO: seguir sus enlaces de texto completo (acotados: decenas
        # de matches es señal de barrido global ruidoso, no de fulltext) y
        # los enlaces que parecen contenido según hints. El BFS irrestricto
        # aquí solo diluiría el presupuesto de crawl.
        if pmap.page_kind == "article":
            for link in pmap.fulltext_links[:5]:
                if link["same_site"] or link["kind"] in ("pdf", "download"):
                    self._track(frontier,
                                CrawlTarget(link["url"], depth=target.depth + 1,
                                            hint_pdf=(link["kind"] in ("pdf", "download")),
                                            from_listing=True),
                                seed, url, "fulltext")
            hints = self.cfg["discovery"]["content_hints"]
            html_links, doc_links = extract_links(result.final_url, result.html)
            for link in doc_links:
                self._track(frontier, CrawlTarget(link, depth=target.depth + 1,
                                                  hint_pdf=True),
                            seed, url, "document_link")
            # En ingesta dirigida (semillas = artículos concretos, p.ej. de
            # search_links) los enlaces "que parecen contenido" del nav son
            # ruido: follow_article_links: false deja solo doc + adjuntos.
            if self.cfg["discovery"].get("follow_article_links", True):
                # Sub-páginas del propio artículo (su ruta EXTIENDE la de
                # esta página: /colecciones/labranza -> /colecciones/labranza/
                # antecedentes): pertenecen al documento, siempre se siguen.
                # Hallazgo del benchmark: 11 capítulos de una colección
                # nunca vistos porque el padre era "article" y no hacía BFS.
                own_path = urlparse(result.final_url).path.rstrip("/") + "/"
                exhaustive = bool(self.cfg["discovery"].get("exhaustive"))
                for link in html_links:
                    child = own_path != "/" and urlparse(link).path.startswith(own_path)
                    if child:
                        self._track(frontier,
                                    CrawlTarget(link, depth=target.depth + 1),
                                    seed, url, "child_page")
                    elif looks_like_content(link, hints):
                        self._track(frontier,
                                    CrawlTarget(link, depth=target.depth + 1),
                                    seed, url, "content_link")
                    elif exhaustive:
                        # Profundidad máxima: un artículo también es un nodo
                        # de navegación (eventos "relacionados", series). El
                        # riel es la saturación, no el tipo de página.
                        self._track(frontier,
                                    CrawlTarget(link, depth=target.depth + 1),
                                    seed, url, "bfs")
            return

        # NAVEGACIÓN u otros: BFS clásico de descubrimiento.
        html_links, pdf_links = extract_links(result.final_url, result.html)
        for link in pdf_links:
            self._track(frontier, CrawlTarget(link, depth=target.depth + 1,
                                              hint_pdf=True),
                        seed, url, "document_link")
        for link in html_links:
            self._track(frontier, CrawlTarget(link, depth=target.depth + 1),
                        seed, url, "bfs")

    # ------------------------------------------------------------------
    def _save_discovered_pdfs(self, result, target: CrawlTarget) -> None:
        for item in result.discovered_pdfs:
            pdf_url = item["url"]
            if self.store.already_saved(make_doc_id(pdf_url)):
                continue
            size_mb = len(item["bytes"]) / 1_048_576
            if size_mb > self.pdf_cfg["max_size_mb"]:
                log.info("PDF (clic) demasiado grande (%.1f MB): %s", size_mb, pdf_url)
                continue
            try:
                doc = extract_pdf_document(
                    pdf_url, item["bytes"],
                    chars_threshold=self.pdf_cfg["ocr_chars_per_page_threshold"],
                    image_ratio_threshold=self.pdf_cfg["ocr_image_coverage_threshold"],
                )
                doc.metadata.fetch_method = "playwright_click"
                doc.metadata.extra["discovered_on"] = result.final_url
                doc.metadata.extra["suggested_filename"] = item.get("filename")
                doc.metadata.depth = target.depth
                doc.metadata.crawl_path = self._path(target.url) + [
                    {"url": pdf_url, "kind": "pdf_click"}]
                self._save_doc(doc)
            except Exception:
                log.exception("Error extrayendo PDF capturado %s", pdf_url)

    # ------------------------------------------------------------------
    def _handle_listing(self, pmap: PageMap, page_url: str, target: CrawlTarget,
                        seed: str, frontier: Frontier) -> None:
        """Serie de artículos: encola cada detalle y sigue la paginación."""
        # Tope de listados EXPANDIDOS por semilla/sección: sin él, ítems que
        # apuntan a subcategorías (también listados) encadenan una cascada
        # de listados que consume el presupuesto sin producir documentos.
        if self._listing_pages >= self.max_listing_pages:
            log.info("Listado %s ignorado (tope de %d listados ya alcanzado)",
                     page_url, self.max_listing_pages)
            return
        self._listing_pages += 1
        new_items = 0
        for item in pmap.listing_items:
            if self._track(frontier, CrawlTarget(item.url, depth=target.depth + 1,
                                                 from_listing=True),
                           seed, page_url, "listing_item"):
                new_items += 1
        log.info("Listado %s: %d ítems (%d nuevos)",
                 page_url, len(pmap.listing_items), new_items)
        # Un listado que descubre ítems nuevos SÍ rinde, aunque no sea un
        # documento: sin esto, 25 páginas seguidas de paginación
        # (noticias?page=3..28) disparaban la saturación y cerraban un sitio
        # que aún tenía cientos de artículos por delante.
        if new_items:
            self._last_yield_fetch = self._site_fetches
        if self._listing_pages >= self.max_listing_pages:
            log.info("Tope de %d páginas de listado alcanzado; no se pagina más",
                     self.max_listing_pages)
            return

        # Paginación (misma profundidad: el listado continúa). Va con
        # prioridad para validar pronto la cobertura, antes de agotar el
        # presupuesto en los detalles.
        for pag_url in pmap.pagination_urls:
            self._track(frontier, CrawlTarget(pag_url, depth=target.depth,
                                              from_listing=True),
                        seed, page_url, "pagination")

        # Paginación JS (sin hrefs): probar la convención ?page=N+1.
        # Se auto-valida: si la página sintetizada no aporta ítems nuevos,
        # su new_items será 0 y no se sintetiza la siguiente.
        if new_items and not pmap.pagination_urls and pmap.pagination_js_only:
            for nxt in synthesize_next_page_urls(page_url):
                log.info("Paginación JS detectada; probando %s", nxt)
                self._track(frontier, CrawlTarget(nxt, depth=target.depth,
                                                  from_listing=True),
                            seed, page_url, "pagination_probe")

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.fetcher.close()
        if self.llm is not None:
            self.llm.close()
