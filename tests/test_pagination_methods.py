"""Batería de métodos de paginación: detección y síntesis.

Cada caso es una convención vista en producción. La lista nació de una
auditoría: de 21 métodos probados, el detector reconocía 12. Los que
faltaban eran, entre otros, la paginación clásica de WordPress (?paged=N),
la de .NET (?pageNumber=N), los enlaces numerados sin parámetro conocido
(/noticias/2) y los textos compuestos ("Página siguiente", "Cargar más").
"""

import unittest

from bs4 import BeautifulSoup

from webharvest.analysis.page_map import (_find_pagination,
                                          synthesize_next_page_urls)

BASE = "https://sitio.cl/noticias"


def find(fragment: str):
    soup = BeautifulSoup(f"<html><body><main>{fragment}</main></body></html>", "lxml")
    return _find_pagination(soup, BASE)


class TestPaginationByParam(unittest.TestCase):
    PARAMS = ["page", "paged", "pagina", "pag", "pageNumber", "pageNum",
              "page_num", "pageIndex", "pg", "start", "offset", "from",
              "first", "desde", "inicio", "seite"]

    def test_every_known_param_name_is_detected(self):
        for name in self.PARAMS:
            urls, _ = find(f'<a href="/noticias?{name}=2">2</a>')
            self.assertTrue(urls, f"no detectó ?{name}=2")

    def test_drupal_zero_based(self):
        urls, _ = find('<a href="/noticias?page=0">1</a>')
        self.assertEqual(urls, ["https://sitio.cl/noticias?page=0"])


class TestPaginationByText(unittest.TestCase):
    LABELS = ["Siguiente", "siguientes", "Página siguiente", "Pág. siguiente",
              "Next", "Next page", "Próxima", "Proxima página", "Seguinte",
              "Ver más", "Cargar más", "Mostrar más", "Load more",
              "»", "›", ">", "→", "More results"]

    def test_next_labels(self):
        for label in self.LABELS:
            urls, _ = find(f'<a href="/noticias/p2">{label}</a>')
            self.assertTrue(urls, f"no detectó texto {label!r}")

    def test_rel_next(self):
        urls, _ = find('<a rel="next" href="/noticias/2">ir</a>')
        self.assertEqual(urls, ["https://sitio.cl/noticias/2"])

    def test_unrelated_labels_are_not_pagination(self):
        for label in ["Más información", "Leer el informe", "Contacto",
                      "Volver", "Anterior", "Inicio"]:
            urls, _ = find(f'<a href="/otra">{label}</a>')
            self.assertFalse(urls, f"falso positivo con {label!r}")


class TestPaginationByNumberedLinks(unittest.TestCase):
    def test_numbers_in_path_without_known_param(self):
        frag = "".join(f'<a href="/noticias/{i}">{i}</a>' for i in (1, 2, 3, 4))
        urls, _ = find(frag)
        self.assertEqual(len(urls), 4)
        self.assertIn("https://sitio.cl/noticias/3", urls)

    def test_year_archive_is_not_pagination(self):
        frag = "".join(f'<a href="/archivo/{y}">{y}</a>' for y in (2024, 2023, 2022))
        urls, _ = find(frag)
        self.assertFalse(urls)

    def test_two_numbers_are_not_enough(self):
        urls, _ = find('<a href="/n/1">1</a><a href="/n/2">2</a>')
        self.assertFalse(urls)


class TestPaginationByPath(unittest.TestCase):
    def test_wordpress_path(self):
        for seg in ("page", "pagina", "pag"):
            urls, _ = find(f'<a href="/noticias/{seg}/2/">2</a>')
            self.assertTrue(urls, f"no detectó /{seg}/2/")


class TestJavascriptOnlyPagination(unittest.TestCase):
    def test_pager_without_links_flags_js(self):
        for cls in ("pager", "pagination", "views-infinite-scroll",
                    "load-more", "cargar-mas", "show-more"):
            urls, js = find(f'<div class="{cls}"><button>más</button></div>')
            self.assertFalse(urls)
            self.assertTrue(js, f"no marcó js_only con class={cls}")


class TestSynthesis(unittest.TestCase):
    def test_index_params_increment_by_one(self):
        self.assertEqual(synthesize_next_page_urls("https://s.cl/n?page=3"),
                         ["https://s.cl/n?page=4"])
        self.assertEqual(synthesize_next_page_urls("https://s.cl/n?paged=2"),
                         ["https://s.cl/n?paged=3"])
        self.assertEqual(synthesize_next_page_urls("https://s.cl/n?pg=5"),
                         ["https://s.cl/n?pg=6"])

    def test_offset_params_double_the_step(self):
        self.assertEqual(synthesize_next_page_urls("https://s.cl/n?start=20"),
                         ["https://s.cl/n?start=40"])
        self.assertEqual(synthesize_next_page_urls("https://s.cl/n?offset=40&cat=3"),
                         ["https://s.cl/n?offset=80&cat=3"])

    def test_unknown_step_is_not_guessed(self):
        self.assertEqual(synthesize_next_page_urls("https://s.cl/n?start=0"), [])

    def test_path_pagination_increments(self):
        self.assertEqual(synthesize_next_page_urls("https://s.cl/blog/page/4/"),
                         ["https://s.cl/blog/page/5/"])

    def test_bare_url_tries_the_common_conventions(self):
        self.assertEqual(synthesize_next_page_urls("https://s.cl/n"),
                         ["https://s.cl/n/page/2/", "https://s.cl/n?page=2",
                          "https://s.cl/n?p=2"])

    def test_already_paginated_does_not_mix_mechanisms(self):
        for u in ("https://s.cl/n?paged=2", "https://s.cl/blog/page/4/"):
            self.assertEqual(len(synthesize_next_page_urls(u)), 1)


if __name__ == "__main__":
    unittest.main()


class TestFalsePositiveGuards(unittest.TestCase):
    """Lo que NO debe entrar como paginación."""

    def test_per_item_read_more_is_not_pagination(self):
        # Listado tipo WordPress/Elementor: cada ítem trae su "Ver más »"
        items = "".join(
            f'<article><h3><a href="/noticias/nota-{i}">Nota {i}</a></h3>'
            f'<a class="elementor-post__read-more" href="/noticias/nota-{i}">'
            f'Ver más »</a></article>' for i in range(10))
        pager = ('<nav class="pagination">'
                 '<a href="/noticias/page/2/">Page 2</a>'
                 '<a href="/noticias/page/2/">Después »</a></nav>')
        urls, _ = find(items + pager)
        self.assertTrue(all("nota-" not in u for u in urls), urls)
        self.assertIn("https://sitio.cl/noticias/page/2/", urls)

    def test_a_single_ver_mas_still_counts(self):
        urls, _ = find('<a href="/noticias/2">Ver más</a>')
        self.assertEqual(urls, ["https://sitio.cl/noticias/2"])

    def test_pagination_must_stay_on_the_same_host(self):
        # caso real: un rel=next que apuntaba al servidor de pruebas del sitio
        urls, _ = find('<a rel="next" href="https://pruebas.sitio.cl/noticias?page=2">n</a>')
        self.assertFalse(urls)

    def test_previous_links_are_not_next(self):
        for label in ["Anterior", "« Anterior", "Previous", "Volver"]:
            urls, _ = find(f'<a href="/noticias?x=1">{label}</a>')
            self.assertFalse(urls, f"falso positivo con {label!r}")


class TestPaginationComputedOutsideListings(unittest.TestCase):
    """La paginación se calcula aunque la página no se clasifique 'listing'.

    Antes solo se buscaba en page_kind == "listing", así que las portadas
    tipo revista y los repositorios (que extraen ítems por la vía del
    "grupo fuerte", con page_kind article/navigation) se quedaban en una
    sola página: era el caso de Cooperativa, Currículum Nacional y la
    Biblioteca Digital del Mineduc.
    """

    def test_article_page_still_reports_its_pager(self):
        from webharvest.analysis.page_map import build_page_map
        body = "<p>" + " ".join(["texto"] * 400) + "</p>"
        html = (f"<html><body><main><article>{body}</article>"
                f'<nav class="pagination"><a href="/portada?page=2">2</a></nav>'
                f"</main></body></html>")
        pm = build_page_map("https://sitio.cl/portada", html, {})
        self.assertEqual(pm.page_kind, "article")
        self.assertIn("https://sitio.cl/portada?page=2", pm.pagination_urls)

    def test_navigation_page_still_reports_its_pager(self):
        from webharvest.analysis.page_map import build_page_map
        links = "".join(f'<a href="/x/{i}">enlace {i}</a>' for i in range(30))
        html = (f"<html><body><main><div>{links}</div>"
                f'<nav class="pager"><a href="/browse?offset=20">Siguiente</a></nav>'
                f"</main></body></html>")
        pm = build_page_map("https://sitio.cl/browse", html, {})
        self.assertNotEqual(pm.page_kind, "listing")
        self.assertIn("https://sitio.cl/browse?offset=20", pm.pagination_urls)

    def test_empty_page_is_not_analysed(self):
        from webharvest.analysis.page_map import build_page_map
        pm = build_page_map("https://sitio.cl/x", "<html><body></body></html>", {})
        self.assertEqual(pm.page_kind, "empty")
        self.assertEqual(pm.pagination_urls, [])
