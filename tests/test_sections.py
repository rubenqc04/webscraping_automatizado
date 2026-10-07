"""Tests del descubridor de secciones de portada."""

import unittest

from webharvest.discovery.sections import discover_sections

HOME = """<html><body>
<header>
  <nav>
    <a href="/">Inicio</a>
    <a href="/pais/">País</a>
    <a href="/mundo/">Mundo</a>
    <a href="/economia/">Economía</a>
    <a href="/deportes/">Deportes</a>
    <a href="/contacto/">Contacto</a>
    <a href="/login">Iniciar sesión</a>
    <a href="https://twitter.com/x">Twitter</a>
  </nav>
</header>
<main>
  <article><a href="/pais/titular-muy-largo-de-una-noticia-cualquiera-2026/">
  Este es un titular largo de una noticia que no es una sección</a></article>
</main>
<footer><a href="/terminos/">Términos</a></footer>
</body></html>"""


class TestDiscoverSections(unittest.TestCase):
    def test_finds_content_sections_only(self):
        secs = discover_sections("https://ejemplo.cl/", HOME)
        names = [s.name for s in secs]
        self.assertEqual(names, ["País", "Mundo", "Economía", "Deportes"])
        self.assertTrue(all(s.source in ("nav", "header") for s in secs))

    def test_excludes_utility_social_and_headlines(self):
        secs = discover_sections("https://ejemplo.cl/", HOME)
        urls = " ".join(s.url for s in secs)
        for bad in ("contacto", "login", "twitter", "terminos", "titular"):
            self.assertNotIn(bad, urls)

    def test_max_sections_cap(self):
        secs = discover_sections("https://ejemplo.cl/", HOME, max_sections=2)
        self.assertEqual(len(secs), 2)


if __name__ == "__main__":
    unittest.main()
