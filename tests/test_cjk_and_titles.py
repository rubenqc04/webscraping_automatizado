"""Tests de count_words (CJK) y prioridad de título og:title > JSON-LD."""

import unittest

from webharvest.extractors.html_extractor import scrape_head_metadata
from webharvest.models import count_words


class TestCountWords(unittest.TestCase):
    def test_latin_matches_split(self):
        self.assertEqual(count_words("hola mundo cruel"), 3)

    def test_cjk_counts_characters(self):
        # 6 caracteres japoneses sin espacios
        self.assertEqual(count_words("日本の歴史です"), 7)

    def test_mixed(self):
        self.assertEqual(count_words("Python は 良い"), 1 + 1 + 2)


class TestTitlePriority(unittest.TestCase):
    def test_og_title_beats_jsonld_headline(self):
        # Wikipedia pone la descripción corta en el headline del JSON-LD
        html = """<html><head>
        <meta property="og:title" content="Historia de Chile">
        <script type="application/ld+json">
        {"@type":"Article","headline":"conjunto de acontecimientos en Chile"}
        </script><title>Historia de Chile - Wikipedia</title>
        </head><body></body></html>"""
        self.assertEqual(scrape_head_metadata(html)["title"], "Historia de Chile")

    def test_jsonld_fallback_when_no_og(self):
        html = """<html><head>
        <script type="application/ld+json">
        {"@type":"Article","headline":"Titular real del artículo"}
        </script></head><body></body></html>"""
        self.assertEqual(scrape_head_metadata(html)["title"],
                         "Titular real del artículo")


if __name__ == "__main__":
    unittest.main()


class TestRightsCapture(unittest.TestCase):
    def test_license_and_rights_meta_captured(self):
        html = """<html><head>
        <meta name="copyright" content="© 2026 Ejemplo. Todos los derechos reservados">
        <link rel="license" href="https://creativecommons.org/licenses/by/4.0/">
        </head><body></body></html>"""
        meta = scrape_head_metadata(html)
        self.assertIn("derechos reservados", meta["rights"])
        self.assertIn("creativecommons.org/licenses/by", meta["license_url"])

    def test_jsonld_license(self):
        html = """<html><head><script type="application/ld+json">
        {"@type":"Article","headline":"x","license":"https://creativecommons.org/licenses/by-nc/4.0/"}
        </script></head><body></body></html>"""
        self.assertIn("by-nc", scrape_head_metadata(html)["license_url"])

    def test_no_rights_is_none(self):
        meta = scrape_head_metadata("<html><head></head><body></body></html>")
        self.assertIsNone(meta["rights"])
        self.assertIsNone(meta["license_url"])
