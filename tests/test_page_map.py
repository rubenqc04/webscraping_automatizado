"""Tests del mapa semántico de página y de la detección de SPA.

Todo con HTML sintético: sin red, sin fixtures pesados.
"""

import unittest

from webharvest.analysis.page_map import build_page_map, synthesize_next_page_url
from webharvest.fetchers.fetchers import needs_dynamic_rendering

BASE = "https://example.org/search?q=x"


def _listing_html(n_items: int = 8) -> str:
    items = "".join(
        f'<li class="result"><h3><a href="/doc/{i}">Título del documento número {i} '
        f'con texto suficiente</a></h3>'
        f'<p>Resumen del documento {i}: lorem ipsum dolor sit amet, consectetur '
        f'adipiscing elit, sed do eiusmod tempor incididunt ut labore.</p>'
        f'<a href="/search?q=x&author={i}">Autor {i}</a></li>'
        for i in range(n_items)
    )
    return (f"<html><body><header><nav><ul>"
            f"<li><a href='/'>Inicio</a></li><li><a href='/about'>Acerca</a></li>"
            f"<li><a href='/help'>Ayuda</a></li></ul></nav></header>"
            f"<main><ul class='results'>{items}</ul>"
            f"<div class='pagination'><span class='action'>1</span>"
            f"<span class='action'>2</span><span class='action'>Next</span></div>"
            f"</main><footer>pie</footer></body></html>")


def _article_html() -> str:
    body = "palabra " * 300
    return (f"<html><body><header><nav><a href='/'>Inicio</a></nav></header>"
            f"<main><article><h1>Un artículo</h1><p>{body}</p>"
            f"<div><h2>Full text links</h2>"
            f"<a href='/files/doc.pdf'>PDF</a>"
            f"<a href='https://doi.org/10.1/abc'>Publisher full text</a></div>"
            f"</article>"
            f"<section><h2>Similares</h2><ul>"
            + "".join(f"<li class='ref'><a href='/doc/{i}'>Referencia {i} con su texto</a>"
                      f" y algo más de contexto del artículo referenciado</li>"
                      for i in range(6))
            + "</ul></section></main></body></html>")


class TestPageMap(unittest.TestCase):
    def test_listing_detected_with_items_and_js_pagination(self):
        pm = build_page_map(BASE, _listing_html())
        self.assertEqual(pm.page_kind, "listing")
        self.assertEqual(len(pm.listing_items), 8)
        # el enlace dominante es el título (no el facetado del autor)
        self.assertTrue(pm.listing_items[0].url.endswith("/doc/0"))
        self.assertIn("Título", pm.listing_items[0].title)
        self.assertTrue(pm.listing_items[0].snippet)
        # paginación sin hrefs -> js_only
        self.assertEqual(pm.pagination_urls, [])
        self.assertTrue(pm.pagination_js_only)

    def test_article_beats_embedded_reference_list(self):
        pm = build_page_map("https://example.org/doc/1", _article_html())
        self.assertEqual(pm.page_kind, "article")
        kinds = {f["kind"] for f in pm.fulltext_links}
        self.assertIn("pdf", kinds)
        # el PDF va primero (vía de extracción más fiable)
        self.assertEqual(pm.fulltext_links[0]["kind"], "pdf")

    def test_nav_menu_is_not_a_listing(self):
        html = ("<html><body><nav><ul>"
                + "".join(f"<li><a href='/sec/{i}'>Sección con nombre largo {i}</a>"
                          f" y descripción del menú de navegación</li>" for i in range(10))
                + "</ul></nav><main><p>bienvenida corta</p></main></body></html>")
        pm = build_page_map("https://example.org/", html)
        self.assertNotEqual(pm.page_kind, "listing")

    def test_empty_page(self):
        pm = build_page_map(BASE, "<html><body></body></html>")
        self.assertEqual(pm.page_kind, "empty")

    def test_content_root_concentration_reported(self):
        pm = build_page_map(BASE, _listing_html())
        self.assertGreater(pm.content_root["text_share"], 0.5)
        self.assertTrue(pm.sections)


class TestSynthesizeNextPage(unittest.TestCase):
    def test_adds_page_param(self):
        self.assertEqual(synthesize_next_page_url("https://a.b/s?q=x"),
                         "https://a.b/s?q=x&page=2")

    def test_increments_existing(self):
        self.assertEqual(synthesize_next_page_url("https://a.b/s?q=x&page=7"),
                         "https://a.b/s?q=x&page=8")

    def test_candidates_include_wordpress_path_style(self):
        from webharvest.analysis import synthesize_next_page_urls
        cands = synthesize_next_page_urls("https://a.b/news/all-posts/")
        self.assertIn("https://a.b/news/all-posts/page/2/", cands)
        self.assertIn("https://a.b/news/all-posts/?page=2", cands)

    def test_existing_path_page_only_continues_that_style(self):
        from webharvest.analysis import synthesize_next_page_urls
        self.assertEqual(synthesize_next_page_urls("https://a.b/news/page/3/"),
                         ["https://a.b/news/page/4/"])


class TestNeedsDynamicRendering(unittest.TestCase):
    MARKERS = ["__NEXT_DATA__", 'id="root"', 'id="app"']

    def test_js_required_message_triggers(self):
        html = ("<html><body><header>" + "menú " * 200 + "</header>"
                "<div>This site requires Javascript to function effectively.</div>"
                "<div id='app'></div></body></html>")
        self.assertTrue(needs_dynamic_rendering(html, 400, self.MARKERS))

    def test_empty_spa_root_triggers_despite_long_menu(self):
        # mucho texto de menú server-rendered, pero la raíz SPA está vacía
        html = ("<html><body><nav>" + "enlace " * 300 + "</nav>"
                "<div id='app'></div></body></html>")
        self.assertTrue(needs_dynamic_rendering(html, 400, self.MARKERS))

    def test_static_content_page_does_not_trigger(self):
        html = "<html><body><article>" + "texto real " * 200 + "</article></body></html>"
        self.assertFalse(needs_dynamic_rendering(html, 400, self.MARKERS))

    def test_populated_spa_root_does_not_trigger(self):
        html = ("<html><body><div id='app'><article>"
                + "contenido renderizado en servidor " * 50
                + "</article></div></body></html>")
        self.assertFalse(needs_dynamic_rendering(html, 400, self.MARKERS))


if __name__ == "__main__":
    unittest.main()


class TestSsrShellDetection(unittest.TestCase):
    def test_huge_markup_tiny_text_with_ng_markers_triggers(self):
        # shell tipo DSpace/Angular: cientos de KB de markup, texto mínimo
        filler = '<div class="ng-star-inserted" ng-version="15.0"></div>' * 2000
        html = f"<html><body>{filler}<footer>pie con enlaces breves</footer></body></html>"
        from webharvest.fetchers.fetchers import needs_dynamic_rendering
        self.assertTrue(needs_dynamic_rendering(html, 400, ['id="app"']))

    def test_big_static_page_with_lots_of_text_does_not_trigger(self):
        body = "<p>" + "texto real de un artículo largo. " * 3000 + "</p>"
        html = f"<html><body data-reactroot='x'>{body}</body></html>"
        from webharvest.fetchers.fetchers import needs_dynamic_rendering
        self.assertFalse(needs_dynamic_rendering(html, 400, ['id="app"']))


class TestListingItemAnchor(unittest.TestCase):
    """El enlace del ítem no es el más largo: el recinto compartido no gana."""

    def test_shared_venue_link_does_not_steal_the_item(self):
        from webharvest.analysis.page_map import build_page_map
        cards = ""
        for i in range(6):
            cards += (
                f'<div class="views-row"><a href="/eventos/evento-{i}"><img src="x.jpg"></a>'
                f'<h3><a href="/eventos/evento-{i}">Taller {i}</a></h3>'
                f'<a href="/recintos/museo-regional-de-la-araucania">Museo Regional de la Araucanía</a>'
                f'<p>Descripción breve del taller número {i} con algo de texto adicional.</p></div>')
        html = f"<html><body><main><div class='view'>{cards}</div></main></body></html>"
        pm = build_page_map("https://e.org/eventos/pasados?page=1", html, {})
        urls = sorted(it.url for it in pm.listing_items)
        self.assertEqual(urls, [f"https://e.org/eventos/evento-{i}" for i in range(6)])
