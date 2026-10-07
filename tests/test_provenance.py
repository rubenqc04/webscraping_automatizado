"""Tests del grafo de procedencia y reconstrucción de crawl_path."""

import tempfile
import unittest
from pathlib import Path

from webharvest.discovery.crawler import CrawlTarget, Frontier
from webharvest.pipeline import ScrapePipeline


def make_pipeline() -> ScrapePipeline:
    tmp = Path(tempfile.mkdtemp())
    cfg = {
        "identity": {"user_agent": "test/1.0"},
        "compliance": {"respect_robots_txt": False, "default_crawl_delay": 0,
                       "max_requests_per_domain": 10},
        "fetching": {"timeout_seconds": 5, "max_retries": 1,
                     "dynamic_detection": {"min_text_length": 400, "spa_markers": []},
                     "playwright": {"headless": True, "wait_until": "load",
                                    "extra_wait_ms": 0, "scroll_passes": 0}},
        "discovery": {"max_depth": 3, "max_pages_per_site": 50,
                      "follow_subdomains": False, "use_sitemaps": False,
                      "content_hints": [], "exclude_patterns": []},
        "extraction": {"min_words_for_valid_article": 10,
                       "pdf": {"download": False, "max_size_mb": 10,
                               "ocr_chars_per_page_threshold": 120,
                               "ocr_image_coverage_threshold": 0.85}},
        "storage": {"base_dir": str(tmp), "markdown_dir": "md", "pdf_dir": "pdf",
                    "metadata_dir": "meta", "ocr_pending_file": "ocr/q.json",
                    "master_index": "meta/index.json"},
    }
    return ScrapePipeline(cfg)


class TestProvenance(unittest.TestCase):
    def test_track_and_path_chain(self):
        p = make_pipeline()
        f = Frontier(p.cfg["discovery"])
        seed = "https://x.org/"
        p._track(f, CrawlTarget(seed, depth=0), seed, None, "seed")
        p._track(f, CrawlTarget("https://x.org/pais/", depth=0), seed,
                 seed, "section", section="País")
        p._track(f, CrawlTarget("https://x.org/pais/nota-1/", depth=1), seed,
                 "https://x.org/pais/", "listing_item")
        p._track(f, CrawlTarget("https://x.org/doc.pdf", depth=2, hint_pdf=True),
                 seed, "https://x.org/pais/nota-1/", "document_link")

        path = p._path("https://x.org/doc.pdf")
        self.assertEqual([s["kind"] for s in path],
                         ["seed", "section", "listing_item", "document_link"])
        self.assertEqual(path[1]["section"], "País")

    def test_duplicate_url_not_retracked(self):
        p = make_pipeline()
        f = Frontier(p.cfg["discovery"])
        seed = "https://x.org/"
        self.assertTrue(p._track(f, CrawlTarget("https://x.org/a"), seed, seed, "bfs"))
        # segunda vía de descubrimiento: la primera procedencia se conserva
        self.assertFalse(p._track(f, CrawlTarget("https://x.org/a"), seed,
                                  "https://x.org/otro", "listing_item"))
        self.assertEqual(p._prov["https://x.org/a"]["kind"], "bfs")

    def test_path_survives_cycles(self):
        p = make_pipeline()
        p._prov = {"a": {"via": "b", "kind": "bfs", "depth": 1},
                   "b": {"via": "a", "kind": "bfs", "depth": 1}}
        path = p._path("a")
        self.assertLessEqual(len(path), 2)   # no se cuelga


if __name__ == "__main__":
    unittest.main()
