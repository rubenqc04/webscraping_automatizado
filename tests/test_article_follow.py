"""Los artículos siguen sus sub-páginas siempre, y todo enlace en modo exhaustivo."""

import unittest
from unittest.mock import patch

from webharvest.discovery.crawler import CrawlTarget, Frontier
from webharvest.pipeline import ScrapePipeline

CFG = {
    "identity": {"user_agent": "t"},
    "compliance": {"respect_robots_txt": False, "default_crawl_delay": 0,
                   "max_requests_per_domain": 100},
    "discovery": {"max_depth": 5, "max_pages_per_site": 100, "follow_subdomains": False,
                  "content_hints": ["/noticias"], "exclude_patterns": [],
                  "follow_article_links": True},
    "extraction": {"min_words_for_valid_article": 5, "pdf": {"download": False}},
    "storage": {"base_dir": "", "markdown_dir": "md", "pdf_dir": "pdf",
                "metadata_dir": "meta", "ocr_pending_file": "ocr/q.json",
                "master_index": "meta/index.json", "ledger": False},
}
PAGE = "https://e.org/colecciones/labranza"
HTML = ("<html><body><article><h1>Labranza</h1>" + "<p>" + " ".join(["palabra"] * 300)
        + '</p></article><a href="/colecciones/labranza/antecedentes">cap</a>'
        '<a href="/noticias/una">n</a><a href="/cartelera/otro-evento">ev</a></body></html>')


def _kinds(cfg_discovery_extra):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cfg = {**CFG, "discovery": {**CFG["discovery"], **cfg_discovery_extra},
               "storage": {**CFG["storage"], "base_dir": d}}
        with patch("webharvest.pipeline.AdaptiveFetcher"):
            pipe = ScrapePipeline(cfg)
        seen = {}
        pipe._track = lambda fr, t, seed, via, kind, section=None: seen.setdefault(t.url, kind) or True
        frontier = Frontier(cfg["discovery"])
        from webharvest.fetchers.fetchers import FetchResult
        res = FetchResult(url=PAGE, final_url=PAGE, status_code=200, html=HTML, method="httpx")
        pipe.fetcher.fetch.return_value = res
        pipe._process(CrawlTarget(PAGE, depth=1), "https://e.org/", frontier)
        return seen


class TestArticleFollow(unittest.TestCase):
    def test_children_and_hints_only_by_default(self):
        seen = _kinds({})
        self.assertEqual(seen.get("https://e.org/colecciones/labranza/antecedentes"), "child_page")
        self.assertEqual(seen.get("https://e.org/noticias/una"), "content_link")
        self.assertNotIn("https://e.org/cartelera/otro-evento", seen)

    def test_exhaustive_follows_everything(self):
        seen = _kinds({"exhaustive": True})
        self.assertEqual(seen.get("https://e.org/cartelera/otro-evento"), "bfs")
        self.assertEqual(seen.get("https://e.org/colecciones/labranza/antecedentes"), "child_page")


if __name__ == "__main__":
    unittest.main()
