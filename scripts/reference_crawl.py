#!/usr/bin/env python3
"""Rastreo de referencia independiente, para construir el inventario de un sitio.

Por qué no basta `wget --spider`: de cinco sitios medidos, falló en tres y por
razones distintas — un 403, una violación de segmento (dos veces, incluso
bajando la profundidad) y un certificado que no cubre su propio host. Y hay
una cuarta falla que no es un fallo de wget sino un límite de fondo: en una
aplicación de página única **todas las URLs devuelven el mismo cascarón** y
los enlaces no están en el HTML, así que un rastreador sin JavaScript
inventaría 10 páginas de un sitio que tiene cientos. Justamente la clase de
sitio donde más falta hace medir.

Este rastreador es DELIBERADAMENTE tonto: un recorrido en anchura que pide
páginas y extrae `href`. No comparte nada con la lógica del pipeline — ni la
Frontier con sus prioridades, ni el page_map, ni la detección de listados o
paginación — así que sigue sirviendo como verdad independiente contra la cual
medirlo. Lo único que comparte es la cortesía: un retardo entre peticiones.

Con `--render` usa el navegador para obtener el HTML, y entonces sí ve los
enlaces de una aplicación de página única.

Uso:
    python scripts/reference_crawl.py https://sitio.cl/ -o inventario.txt
    python scripts/reference_crawl.py https://spa.cl/ --render --max-pages 400
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
# no son páginas: binarios de presentación y archivos que no llevan texto propio
SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|svg|webp|ico|css|js|woff2?|ttf|eot|mp[34]|avi|mov|zip|rar)$", re.I)


def same_host(url: str, host: str) -> bool:
    h = urlparse(url).netloc.lower()
    return h == host or h == "www." + host or h.removeprefix("www.") == host


class Renderer:
    """Navegador reutilizado, solo si se pide --render."""

    def __init__(self, timeout: int, wait_ms: int, wait_until: str = "commit"):
        """Por defecto se espera solo a `commit` y luego se aguarda que
        aparezcan enlaces en el DOM.

        Las dos esperas "naturales" fallan en aplicaciones de página única:
        `networkidle` nunca llega si el sitio mantiene sondeo o websockets, y
        `domcontentloaded` tampoco se cumplió en carga fría en un Next.js real
        (agotó 40 s; solo pasaba si otra carga había calentado la caché).
        `commit` devuelve en cuanto la navegación se confirma, y a partir de
        ahí se espera explícitamente el contenido, que es lo que interesa.
        """
        self.timeout, self.wait_ms, self.wait_until = timeout, wait_ms, wait_until
        self._pw = self._browser = None

    def html(self, url: str) -> tuple[str | None, str, int | None]:
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(user_agent=UA, locale="es-ES",
                                        ignore_https_errors=True)
        page = ctx.new_page()
        try:
            resp = page.goto(url, wait_until=self.wait_until,
                             timeout=self.timeout * 1000)
            # el contenido de una SPA aparece después: se espera a que haya
            # enlaces, y si no llegan, se sigue con lo que haya
            try:
                page.wait_for_selector("a[href]", timeout=self.wait_ms * 4,
                                       state="attached")
            except Exception:
                pass
            page.wait_for_timeout(self.wait_ms)
            return page.content(), page.url, (resp.status if resp else None)
        finally:
            ctx.close()

    def close(self):
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()


def crawl(seed: str, max_pages: int, max_depth: int, delay: float,
          render: bool, timeout: int, wait_ms: int, verbose: bool,
          wait_until: str = "commit", out: Path | None = None) -> list[str]:
    """Recorre el sitio y devuelve las páginas encontradas.

    `out` se escribe INCREMENTALMENTE, cada 20 páginas. Sin eso, un rastreo
    cortado por el límite de tiempo del job no dejaba nada: uno llevaba 1.675
    páginas encontradas y 8.504 en cola cuando lo mataron, y se perdió todo.
    """
    host = urlparse(seed).netloc.lower().removeprefix("www.")
    seen: set[str] = set()
    found: list[str] = []
    q: deque[tuple[str, int]] = deque([(seed, 0)])
    renderer = Renderer(timeout, wait_ms, wait_until) if render else None
    client = httpx.Client(headers={"User-Agent": UA}, timeout=timeout,
                          follow_redirects=True, verify=False)
    try:
        while q and len(found) < max_pages:
            url, depth = q.popleft()
            url, _ = urldefrag(url)
            if url in seen:
                continue
            seen.add(url)
            try:
                if renderer is not None:
                    html, final, status = renderer.html(url)
                else:
                    r = client.get(url)
                    status = r.status_code
                    ctype = r.headers.get("content-type", "").lower()
                    html = r.text if "html" in ctype else None
                    final = str(r.url)
            except Exception as exc:
                if verbose:
                    print(f"  ! {type(exc).__name__} {url}", file=sys.stderr)
                continue
            if status is None or status >= 400:
                continue
            found.append(final)
            if out is not None and len(found) % 20 == 0:
                out.write_text("\n".join(found), encoding="utf-8")
            if verbose and len(found) % 25 == 0:
                print(f"  {len(found)} páginas, {len(q)} en cola", file=sys.stderr)
            if html is None or depth >= max_depth:
                time.sleep(delay)
                continue
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                nxt, _ = urldefrag(urljoin(final, href))
                if nxt in seen or not same_host(nxt, host) or SKIP_EXT.search(nxt):
                    continue
                q.append((nxt, depth + 1))
            time.sleep(delay)
    finally:
        client.close()
        if renderer is not None:
            renderer.close()
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("seed")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--max-pages", type=int, default=1500)
    ap.add_argument("--max-depth", type=int, default=6)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--render", action="store_true",
                    help="obtener el HTML con navegador (necesario en SPA)")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--wait-ms", type=int, default=1800)
    ap.add_argument("--wait-until", default="commit",
                    choices=["commit", "domcontentloaded", "load", "networkidle"])
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()

    t0 = time.time()
    urls = crawl(a.seed, a.max_pages, a.max_depth, a.delay, a.render,
                 a.timeout, a.wait_ms, not a.quiet, a.wait_until, a.out)
    a.out.write_text("\n".join(urls), encoding="utf-8")
    print(f"{len(urls)} páginas en {time.time()-t0:.0f}s "
          f"({'con navegador' if a.render else 'sin navegador'}) -> {a.out}")


if __name__ == "__main__":
    main()
