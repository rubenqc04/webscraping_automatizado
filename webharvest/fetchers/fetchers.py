"""Capa de obtención de contenido (fetchers).

Estrategia adaptativa en cascada ("escalation ladder"):

    1. StaticFetcher (httpx)  -> barato y rápido; sirve para la mayoría
                                  de sitios de noticias, blogs y gob.
    2. Heurística de detección -> ¿el HTML estático trae contenido real
                                  o es un cascarón de SPA (React/Vue/
                                  Next/Angular)?
    3. DynamicFetcher (Playwright) -> renderiza JS, hace scroll para
                                  lazy-loading y devuelve el DOM final.

El resultado de ambos caminos es un `FetchResult` homogéneo, de modo
que el resto del pipeline no necesita saber cómo se obtuvo el HTML.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ..compliance.policy import BlockCheck, BlockDetector

log = logging.getLogger("webharvest.fetchers")


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: Optional[int]
    content_type: str = ""
    html: Optional[str] = None
    body_bytes: Optional[bytes] = None      # para PDFs u otros binarios
    method: str = "httpx"                    # httpx | playwright
    block: BlockCheck = field(default_factory=lambda: BlockCheck(False))
    # PDFs capturados al sondear acciones JS tipo "Open PDF" (sin href).
    # Cada uno: {"url": ..., "filename": ..., "bytes": ...}
    discovered_pdfs: list[dict] = field(default_factory=list)

    @property
    def is_pdf(self) -> bool:
        if "application/pdf" in self.content_type:
            return True
        if self.body_bytes and self.body_bytes[:5] == b"%PDF-":
            return True
        return False

    @property
    def is_word(self) -> bool:
        """Word moderno (.docx, OOXML/zip) o legado (.doc, OLE2)."""
        if "officedocument.wordprocessingml" in self.content_type \
                or "application/msword" in self.content_type:
            return True
        if not self.body_bytes:
            return False
        head = self.body_bytes[:8]
        if head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":     # OLE2 (.doc)
            return True
        if head[:4] == b"PK\x03\x04" and self.url.lower().split("?")[0].endswith(".docx"):
            return True
        return False


# ----------------------------------------------------------------------
# 1) Fetcher estático
# ----------------------------------------------------------------------
class StaticFetcher:
    def __init__(self, user_agent: str, timeout: int = 30, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
            },
            follow_redirects=True,
            timeout=timeout,
        )

    def fetch(self, url: str) -> FetchResult:
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1.5, min=2, max=30),
            retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
            reraise=True,
        )
        def _do() -> httpx.Response:
            return self.client.get(url)

        try:
            resp = _do()
        except Exception as exc:
            log.warning("Fallo de red en %s: %s", url, exc)
            return FetchResult(url, url, None, block=BlockCheck(True, "network", str(exc)))

        ctype = resp.headers.get("content-type", "").lower()
        # RDF/Atom/RSS son metadata, no contenido para el extractor de HTML
        is_htmlish = "html" in ctype or ("xml" in ctype and not any(
            k in ctype for k in ("rdf", "rss", "atom")))
        html = resp.text if is_htmlish else None
        block = BlockDetector.check(resp.status_code, html)
        return FetchResult(
            url=url,
            final_url=str(resp.url),
            status_code=resp.status_code,
            content_type=ctype,
            html=html,
            body_bytes=resp.content if "html" not in ctype else None,
            method="httpx",
            block=block,
        )

    def close(self) -> None:
        self.client.close()


# ----------------------------------------------------------------------
# 2) Heurística: ¿necesita render JS?
# ----------------------------------------------------------------------
# Mensajes "necesitas JavaScript" (es/en/pt): señal directa de cascarón SPA.
_JS_REQUIRED_PAT = re.compile(
    r"requires?\s+javascript|enable\s+javascript|javascript\s+is\s+"
    r"(currently\s+)?(disabled|turned\s+off)|activa\w*\s+javascript"
    r"|necesita\s+javascript|habilit\w+\s+(o\s+)?javascript", re.I)

# Contenedores raíz típicos de SPA (React/Vue/Next/Angular/Gatsby).
_SPA_ROOT_SELECTORS = [
    "#app", "#root", "#__next", "#___gatsby", "[data-reactroot]",
    "[ng-app]", "app-root",
]


def needs_dynamic_rendering(html: str, min_text_length: int, spa_markers: list[str]) -> bool:
    """Decide si el HTML estático es un cascarón de SPA.

    Señales (cualquiera dispara el render dinámico):
      a) La página lo dice: mensajes tipo "requires JavaScript".
      b) La raíz de la SPA (#app, #root, #__next...) existe pero está
         (casi) vacía de texto: el menú/footer server-rendered puede sumar
         mucho texto y aún así el contenido real no estar ahí.
      c) Señal clásica: poco texto visible + marcadores de framework.
    """
    if not html:
        return True

    soup = BeautifulSoup(html, "lxml")

    js_msg = any(_JS_REQUIRED_PAT.search(ns.get_text(" "))
                 for ns in soup.find_all("noscript"))

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    visible_text = " ".join(soup.get_text(" ").split())

    # a) mensaje "requiere JavaScript" — solo cuenta si además hay poco
    #    texto visible: muchas páginas SSR completas conservan ese
    #    boilerplate en <noscript> y no necesitan render.
    if (js_msg or _JS_REQUIRED_PAT.search(visible_text)) \
            and len(visible_text) < min_text_length * 3:
        return True

    # a') página prácticamente vacía: sin texto no hay nada que perder
    #     probando el render, tenga o no marcadores de framework
    if len(visible_text) < 100:
        return True

    # b) raíz de SPA presente pero casi vacía
    for sel in _SPA_ROOT_SELECTORS:
        try:
            node = soup.select_one(sel)
        except Exception:
            continue
        if node is not None:
            root_text = " ".join(node.get_text(" ").split())
            if len(root_text) < max(200, min_text_length // 2):
                return True

    # c) poco texto visible + marcadores de framework => probable SPA
    html_low = html.lower()
    has_marker = (any(m.lower() in html_low for m in spa_markers)
                  or re.search(r"\bng-version|\bng-star-inserted|data-reactroot"
                               r"|__nuxt|__next|data-v-app", html_low))
    too_short = len(visible_text) < min_text_length

    if too_short and has_marker:
        return True
    if too_short and html_low.count("<script") > 10:
        return True

    # d) shell SSR de framework: HTML enorme pero casi sin texto visible
    #    (Angular/DSpace y similares sirven cientos de KB de markup con
    #    claves i18n sin resolver; el texto real llega tras el bootstrap JS)
    if len(html) > 50_000 and has_marker \
            and len(visible_text) / len(html) < 0.01:
        return True
    return False


# ----------------------------------------------------------------------
# 3) Fetcher dinámico (Playwright) — carga perezosa (lazy import)
# ----------------------------------------------------------------------
class DynamicFetcher:
    """Renderiza páginas con Chromium headless.

    Se instancia una sola vez y reutiliza el navegador entre páginas
    (lanzar Chromium es lo caro; abrir pestañas es barato).
    """

    def __init__(self, user_agent: str, headless: bool = True,
                 wait_until: str = "networkidle", extra_wait_ms: int = 1500,
                 scroll_passes: int = 3, timeout: int = 30,
                 pdf_click_probe: bool = False):
        self.user_agent = user_agent
        self.headless = headless
        self.wait_until = wait_until
        self.extra_wait_ms = extra_wait_ms
        self.scroll_passes = scroll_passes
        self.timeout_ms = timeout * 1000
        self.pdf_click_probe = pdf_click_probe
        self._pw = None
        self._browser = None

    def _ensure_browser(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)

    def fetch(self, url: str) -> FetchResult:
        self._ensure_browser()
        context = self._browser.new_context(
            user_agent=self.user_agent,
            locale="es-ES",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        try:
            resp = page.goto(url, wait_until=self.wait_until, timeout=self.timeout_ms)
            # Scroll para disparar lazy-loading / infinite scroll moderado
            for _ in range(self.scroll_passes):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(600)
            page.wait_for_timeout(self.extra_wait_ms)
            self._wait_text_stability(page)

            html = page.content()
            status = resp.status if resp else None
            block = BlockDetector.check(status, html)

            discovered = []
            if self.pdf_click_probe and not block.is_blocked:
                discovered = self._probe_pdf_actions(page)

            return FetchResult(
                url=url,
                final_url=page.url,
                status_code=status,
                content_type="text/html",
                html=html,
                method="playwright",
                block=block,
                discovered_pdfs=discovered,
            )
        except Exception as exc:
            log.warning("Playwright falló en %s: %s", url, exc)
            return FetchResult(url, url, None, method="playwright",
                               block=BlockCheck(True, "render_error", str(exc)))
        finally:
            context.close()

    @staticmethod
    def _wait_text_stability(page, polls: int = 8, interval_ms: int = 700,
                             stable_needed: int = 2) -> None:
        """Espera a que el texto visible deje de crecer.

        `networkidle` no basta para SPAs que hidratan tarde (Angular/DSpace
        muestran claves i18n sin resolver durante segundos). Se muestrea el
        largo del texto del body hasta verlo estable dos veces seguidas.
        """
        last, stable = -1, 0
        for _ in range(polls):
            try:
                n = page.evaluate("() => (document.body.innerText || '').length")
            except Exception:
                return
            if n == last:
                stable += 1
                if stable >= stable_needed:
                    return
            else:
                stable = 0
            last = n
            page.wait_for_timeout(interval_ms)

    # Acciones "PDF" sin href (botones/spans manejados por JS): se hace
    # clic y se captura la descarga resultante. Los bytes se toman en el
    # momento porque estos enlaces suelen ser URLs firmadas que expiran.
    _PDF_ACTION_PAT = re.compile(
        r"^\s*(open|download|view|get|abrir|descargar|ver|baixar)?\s*"
        r"(the\s+)?(full\s*-?\s*text\s+)?pdf\s*(file|document)?\s*$", re.I)

    def _probe_pdf_actions(self, page, max_clicks: int = 2) -> list[dict]:
        found: list[dict] = []
        try:
            candidates = page.get_by_text(self._PDF_ACTION_PAT).all()
        except Exception:
            return found
        # muchos "PDF" repetidos = probablemente un listado: no hacer clic
        if not candidates or len(candidates) > 4:
            return found

        seen_urls: set[str] = set()
        for el in candidates[: max_clicks + 2]:
            if len(found) >= max_clicks:
                break
            try:
                # si en realidad es un <a href>, ya lo recogió el análisis de enlaces
                if el.evaluate("n => !!n.closest('a[href]')"):
                    continue
                with page.expect_download(timeout=8000) as dl_info:
                    el.click(timeout=3000)
                dl = dl_info.value
                if dl.url in seen_urls:
                    continue
                seen_urls.add(dl.url)
                path = dl.path()          # espera a que termine la descarga
                data = open(path, "rb").read() if path else b""
                if data[:5] == b"%PDF-":
                    found.append({"url": dl.url,
                                  "filename": dl.suggested_filename,
                                  "bytes": data})
                    log.info("PDF capturado vía clic (%d KB): %s",
                             len(data) // 1024, dl.suggested_filename)
            except Exception:
                continue
        return found

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self._browser = self._pw = None


# ----------------------------------------------------------------------
# Fachada adaptativa
# ----------------------------------------------------------------------
class AdaptiveFetcher:
    """Punto de entrada único: intenta estático y escala a dinámico."""

    def __init__(self, cfg: dict):
        ident = cfg["identity"]
        f = cfg["fetching"]
        self.static = StaticFetcher(ident["user_agent"], f["timeout_seconds"], f["max_retries"])
        self.dynamic = DynamicFetcher(
            ident["user_agent"],
            headless=f["playwright"]["headless"],
            wait_until=f["playwright"]["wait_until"],
            extra_wait_ms=f["playwright"]["extra_wait_ms"],
            scroll_passes=f["playwright"]["scroll_passes"],
            timeout=f["timeout_seconds"],
            pdf_click_probe=f["playwright"].get("pdf_click_probe", False),
        )
        self.min_text = f["dynamic_detection"]["min_text_length"]
        self.spa_markers = f["dynamic_detection"]["spa_markers"]

    def fetch(self, url: str) -> FetchResult:
        result = self.static.fetch(url)

        # Binarios (PDF) o bloqueos: no tiene sentido renderizar
        if result.is_pdf or result.block.is_blocked:
            return result

        if result.html and needs_dynamic_rendering(result.html, self.min_text, self.spa_markers):
            log.info("Escalando a render dinámico: %s", url)
            try:
                dyn = self.dynamic.fetch(url)
            except Exception as exc:
                # El navegador puede no estar disponible en el nodo (en el
                # cluster, /tmp con cuota agotada hacía fallar el mkdtemp de
                # Playwright). Eso degrada la calidad de ESTA página, no debe
                # tumbar el crawl del sitio: se sigue con el HTML estático.
                log.warning("Render dinámico no disponible (%s: %s); "
                            "se usa el HTML estático de %s",
                            type(exc).__name__, exc, url)
                return result
            # Si el render también falla, conservamos lo estático como fallback
            if dyn.html and not dyn.block.is_blocked:
                return dyn
        return result

    def close(self) -> None:
        self.static.close()
        self.dynamic.close()
