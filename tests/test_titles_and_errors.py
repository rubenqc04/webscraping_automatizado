"""Títulos de binarios por nombre de archivo, y respuestas de error HTTP."""

import tempfile
import unittest
from unittest.mock import patch

from webharvest.discovery.crawler import CrawlTarget, Frontier
from webharvest.pipeline import ScrapePipeline, _title_from_filename

CFG = {
    "identity": {"user_agent": "t"},
    "compliance": {"respect_robots_txt": False, "default_crawl_delay": 0,
                   "max_requests_per_domain": 100},
    "discovery": {"max_depth": 5, "max_pages_per_site": 100, "follow_subdomains": False,
                  "content_hints": [], "exclude_patterns": []},
    "extraction": {"min_words_for_valid_article": 5, "pdf": {"download": True,
                   "max_size_mb": 40, "ocr_chars_per_page_threshold": 100,
                   "ocr_image_coverage_threshold": 0.8}},
    "storage": {"base_dir": "", "markdown_dir": "md", "pdf_dir": "pdf",
                "metadata_dir": "meta", "ocr_pending_file": "ocr/q.json",
                "master_index": "meta/index.json", "ledger": False},
}


class TestTitleFromFilename(unittest.TestCase):
    def test_descriptive_filenames_become_titles(self):
        self.assertEqual(
            _title_from_filename("https://x.cl/f/2021-07/GUIA%20EDUCATIVA%20DESCARGABLE.pdf"),
            "Guia Educativa Descargable")
        self.assertEqual(
            _title_from_filename("https://x.cl/f/Protocolo_servicios_educativos.docx"),
            "Protocolo servicios educativos")

    def test_generic_cms_names_are_rejected(self):
        for u in ("https://x.cl/642/articles-90118_archivo_01.pdf",
                  "https://x.cl/a/file_2.pdf", "https://x.cl/doc/1234.pdf",
                  "https://x.cl/a/b.pdf", "https://x.cl/descarga.pdf"):
            self.assertIsNone(_title_from_filename(u), u)


class TestHttpErrorNotADocument(unittest.TestCase):
    def _process(self, status, html):
        from webharvest.fetchers.fetchers import FetchResult
        with tempfile.TemporaryDirectory() as d:
            cfg = {**CFG, "storage": {**CFG["storage"], "base_dir": d}}
            with patch("webharvest.pipeline.AdaptiveFetcher"):
                pipe = ScrapePipeline(cfg)
            url = "https://e.org/roto"
            pipe.fetcher.fetch.return_value = FetchResult(
                url=url, final_url=url, status_code=status, html=html, method="httpx")
            pipe._process(CrawlTarget(url, depth=1), "https://e.org/",
                          Frontier(cfg["discovery"]))
            return pipe.store.stats()["total_documents"]

    def test_404_error_page_is_not_saved(self):
        page = ("<html><body><h1>Página no encontrada</h1><p>" + " ".join(
            ["lo", "sentimos", "la", "pagina", "buscada", "no", "existe"] * 20)
            + "</p></body></html>")
        self.assertEqual(self._process(404, page), 0)

    def test_200_page_is_saved(self):
        page = ("<html><body><article><h1>Nota real</h1><p>"
                + " ".join(["palabra"] * 200) + "</p></article></body></html>")
        self.assertEqual(self._process(200, page), 1)


if __name__ == "__main__":
    unittest.main()
