"""Tests del orquestador paralelo (agrupación, resume, aislamiento)."""

import json
import tempfile
import unittest
from pathlib import Path

from webharvest.parallel import ParallelHarvester


def base_cfg(tmp: Path) -> dict:
    return {
        "identity": {"user_agent": "test/1.0"},
        "compliance": {"respect_robots_txt": False, "default_crawl_delay": 0,
                       "max_requests_per_domain": 10},
        "fetching": {"timeout_seconds": 5, "max_retries": 1,
                     "dynamic_detection": {"min_text_length": 400, "spa_markers": []},
                     "playwright": {"headless": True, "wait_until": "load",
                                    "extra_wait_ms": 0, "scroll_passes": 0}},
        "discovery": {"max_depth": 1, "max_pages_per_site": 5,
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


class TestGrouping(unittest.TestCase):
    def test_groups_by_domain_biggest_first(self):
        groups = ParallelHarvester.group_by_domain([
            "https://a.cl/1", "https://b.cl/1", "https://a.cl/2", "https://a.cl/3",
            "https://b.cl/2", "https://c.cl/1",
        ])
        self.assertEqual(list(groups), ["a.cl", "b.cl", "c.cl"])   # 3, 2, 1
        self.assertEqual(len(groups["a.cl"]), 3)

    def test_ignores_malformed_urls(self):
        groups = ParallelHarvester.group_by_domain(["not-a-url", "https://a.cl/x"])
        self.assertEqual(list(groups), ["a.cl"])

    def test_case_insensitive_host(self):
        groups = ParallelHarvester.group_by_domain(
            ["https://A.CL/1", "https://a.cl/2"])
        self.assertEqual(list(groups), ["a.cl"])
        self.assertEqual(len(groups["a.cl"]), 2)


class TestResume(unittest.TestCase):
    def test_completed_domains_are_skipped(self):
        tmp = Path(tempfile.mkdtemp())
        h = ParallelHarvester(base_cfg(tmp), workers=2)
        h._mark_done("ya.cl")
        # nueva instancia con --resume: lee el progreso del disco
        h2 = ParallelHarvester(base_cfg(tmp), workers=2, resume=True)
        self.assertIn("ya.cl", h2._done)
        saved = json.loads((tmp / "_progress/completed_domains.json").read_text())
        self.assertEqual(saved, ["ya.cl"])

    def test_without_resume_progress_is_ignored(self):
        tmp = Path(tempfile.mkdtemp())
        ParallelHarvester(base_cfg(tmp), workers=2)._mark_done("ya.cl")
        self.assertEqual(ParallelHarvester(base_cfg(tmp), workers=2)._done, set())

    def test_shared_components_injected_into_workers(self):
        tmp = Path(tempfile.mkdtemp())
        h = ParallelHarvester(base_cfg(tmp), workers=2)
        from webharvest.pipeline import ScrapePipeline
        p = ScrapePipeline(h.cfg, store=h.store, throttle=h.throttle,
                           robots=h.robots)
        try:
            self.assertIs(p.store, h.store)
            self.assertIs(p.throttle, h.throttle)
            self.assertIs(p.robots, h.robots)
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
