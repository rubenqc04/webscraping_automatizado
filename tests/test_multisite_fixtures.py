"""Regresión multi-sitio con HTML real capturado (2026-08-28, gzip).

Cada fixture es una página real; los asserts fijan el comportamiento
observado que ya validamos en vivo. Si una heurística genérica cambia
y rompe un sitio que funcionaba, esto lo detecta sin tocar la red.

Fixtures:
    search_rendered      Europe PMC, búsqueda renderizada  -> listing (25 ítems, paginación JS)
    article_med          Europe PMC, artículo renderizado  -> article + fulltext DOI
    search_browser       Europe PMC, cascarón SPA sin render -> debe escalar a dinámico
    cepal_discover       DSpace/Angular, shell SSR gigante   -> debe escalar a dinámico
    cepal_home_rendered  DSpace home renderizado             -> navigation (sin ítems)
"""

import gzip
import unittest
from pathlib import Path

from webharvest.analysis import build_page_map
from webharvest.fetchers.fetchers import needs_dynamic_rendering

FIXTURES = Path(__file__).parent / "fixtures"
SPA_MARKERS = ["__NEXT_DATA__", "ng-app", "data-reactroot", 'id="root"', 'id="app"', "nuxt"]


def load(name: str) -> str:
    return gzip.decompress((FIXTURES / f"{name}.html.gz").read_bytes()).decode("utf-8")


class TestEuropePmc(unittest.TestCase):
    def test_search_is_listing_with_items_and_js_pagination(self):
        pm = build_page_map("https://europepmc.org/search?query=SRC%3A%2a", load("search_rendered"))
        self.assertEqual(pm.page_kind, "listing")
        self.assertGreaterEqual(len(pm.listing_items), 20)
        self.assertTrue(all(i.url and i.title for i in pm.listing_items))
        self.assertTrue(pm.pagination_js_only)

    def test_article_with_publisher_fulltext(self):
        pm = build_page_map("https://europepmc.org/article/MED/42557168", load("article_med"))
        self.assertEqual(pm.page_kind, "article")
        self.assertTrue(any("doi.org" in f["url"] for f in pm.fulltext_links))

    def test_spa_shell_escalates(self):
        self.assertTrue(needs_dynamic_rendering(load("search_browser"), 400, SPA_MARKERS))

    def test_rendered_search_does_not_escalate(self):
        self.assertFalse(needs_dynamic_rendering(load("search_rendered"), 400, SPA_MARKERS))


class TestDspaceCepal(unittest.TestCase):
    def test_ssr_shell_escalates(self):
        self.assertTrue(needs_dynamic_rendering(load("cepal_discover"), 400, SPA_MARKERS))

    def test_rendered_home_is_navigation_not_listing(self):
        pm = build_page_map("https://repositorio.cepal.org/", load("cepal_home_rendered"))
        self.assertNotEqual(pm.page_kind, "listing")


if __name__ == "__main__":
    unittest.main()
