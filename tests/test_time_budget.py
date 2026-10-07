"""Presupuesto de tiempo por sitio, junto a la saturación."""

import tempfile
import unittest
from unittest.mock import patch

from webharvest.pipeline import ScrapePipeline

CFG = {
    "identity": {"user_agent": "t"},
    "compliance": {"respect_robots_txt": False, "default_crawl_delay": 0,
                   "max_requests_per_domain": 100},
    "discovery": {"max_depth": 3, "max_pages_per_site": 100,
                  "follow_subdomains": False, "content_hints": [],
                  "exclude_patterns": [], "saturation_window": 25},
    "extraction": {"min_words_for_valid_article": 5, "pdf": {"download": False}},
    "storage": {"base_dir": "", "markdown_dir": "md", "pdf_dir": "pdf",
                "metadata_dir": "meta", "ocr_pending_file": "ocr/q.json",
                "master_index": "meta/index.json", "ledger": False},
}


def _pipe(extra_discovery=None):
    d = tempfile.mkdtemp()
    cfg = {**CFG,
           "discovery": {**CFG["discovery"], **(extra_discovery or {})},
           "storage": {**CFG["storage"], "base_dir": d}}
    with patch("webharvest.pipeline.AdaptiveFetcher"):
        return ScrapePipeline(cfg)


class TestTimeBudget(unittest.TestCase):
    def test_absent_by_default(self):
        p = _pipe()
        self.assertIsNone(p.max_seconds_per_site)
        p._start_site_clock()
        self.assertIsNone(p._saturated())

    def test_closes_the_site_when_the_clock_runs_out(self):
        p = _pipe({"max_seconds_per_site": 60})
        p._start_site_clock()
        self.assertIsNone(p._saturated())          # recién empezado
        p._site_started -= 61                       # como si hubieran pasado 61 s
        reason = p._saturated()
        self.assertIsNotNone(reason)
        self.assertIn("tiempo", reason)

    def test_saturation_still_wins_when_it_applies_first(self):
        p = _pipe({"max_seconds_per_site": 10_000, "saturation_window": 3})
        p._start_site_clock(); p._reset_site_counters()
        p._site_fetches = 5          # 5 obtenciones sin rendir nada
        reason = p._saturated()
        self.assertIn("sin documentos nuevos", reason)

    def test_section_reset_does_not_restart_the_site_clock(self):
        """El bug que esto cubre: con el reloj dentro de los contadores de
        saturación, cada sección obtenía el presupuesto completo del sitio."""
        p = _pipe({"max_seconds_per_site": 60})
        p._start_site_clock()
        p._site_started -= 61
        self.assertIsNotNone(p._saturated())
        p._reset_site_counters()            # como al entrar a otra sección
        self.assertIsNotNone(p._saturated(), "la sección nueva reinició el reloj")

    def test_clock_resets_between_sites(self):
        p = _pipe({"max_seconds_per_site": 60})
        p._start_site_clock()
        p._site_started -= 61
        self.assertIsNotNone(p._saturated())
        p._start_site_clock()                       # siguiente sitio
        self.assertIsNone(p._saturated())


if __name__ == "__main__":
    unittest.main()
