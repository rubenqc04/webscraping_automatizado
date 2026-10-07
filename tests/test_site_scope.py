"""Alcance de un sitio y cierres en la portada.

En la corrida v3, 21 de los primeros 47 sitios terminados cerraron con 0
documentos. Tres causas venían del pipeline y no del sitio:
- www.: la semilla www.chile.travel y sus enlaces a chile.travel se
  trataban como sitios distintos;
- redirección: junji.gob.cl -> junji.cl dejaba todo "offsite";
- modo secciones sin menú: 0 secciones = 0 páginas recorridas.
Y los cierres en la portada (robots, red, 4xx) no quedaban en el libro.
"""

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from webharvest.compliance.policy import BlockCheck
from webharvest.discovery.crawler import CrawlTarget, Frontier, same_site
from webharvest.fetchers.fetchers import FetchResult
from webharvest.pipeline import ScrapePipeline

DISC = {"max_depth": 3, "max_pages_per_site": 100, "follow_subdomains": False,
        "content_hints": [], "exclude_patterns": [], "saturation_window": 25}
CFG = {
    "identity": {"user_agent": "t"},
    "compliance": {"respect_robots_txt": False, "default_crawl_delay": 0,
                   "max_requests_per_domain": 100},
    "discovery": DISC,
    "extraction": {"min_words_for_valid_article": 5, "pdf": {"download": False}},
    "storage": {"base_dir": "", "markdown_dir": "md", "pdf_dir": "pdf",
                "metadata_dir": "meta", "ocr_pending_file": "ocr/q.json",
                "master_index": "meta/index.json", "ledger": False},
}


def _pipe(mode="sections"):
    cfg = {**CFG, "discovery": {**DISC, "mode": mode},
           "storage": {**CFG["storage"], "base_dir": tempfile.mkdtemp()}}
    with patch("webharvest.pipeline.AdaptiveFetcher"):
        p = ScrapePipeline(cfg)
    p.notes = []
    p._note = lambda url, decision, **kw: p.notes.append((url, decision, kw.get("reason")))
    return p


class TestSameSite(unittest.TestCase):
    def test_www_does_not_matter(self):
        self.assertTrue(same_site("https://chile.travel/zonas/", "https://www.chile.travel", False))
        self.assertTrue(same_site("https://www.junji.cl/a", "https://junji.cl", False))

    def test_other_hosts_still_offsite(self):
        self.assertFalse(same_site("https://facebook.com/x", "https://www.chile.travel", False))
        self.assertFalse(same_site("https://junji.cl/a", "https://www.junji.gob.cl", False))

    def test_aliases(self):
        self.assertTrue(same_site("https://junji.cl/a", "https://www.junji.gob.cl", False, {"junji.cl"}))

    def test_frontier_sees_an_alias_added_after_creation(self):
        hosts = set()
        f = Frontier(DISC, site_hosts=hosts)
        self.assertFalse(f.add(CrawlTarget("https://junji.cl/a", depth=1), "https://www.junji.gob.cl"))
        hosts.add("junji.cl")
        self.assertTrue(f.add(CrawlTarget("https://junji.cl/b", depth=1), "https://www.junji.gob.cl"))


class TestSeedRedirect(unittest.TestCase):
    def test_redirect_of_the_seed_widens_the_scope(self):
        p = _pipe()
        p._adopt_redirect("https://www.junji.gob.cl", "https://junji.cl/")
        self.assertIn("junji.cl", p._site_hosts)

    def test_www_only_redirect_adds_nothing(self):
        p = _pipe()
        p._adopt_redirect("https://www.chile.travel", "https://chile.travel/")
        self.assertEqual(p._site_hosts, set())

    def test_scope_resets_between_sites(self):
        p = _pipe()
        p._site_hosts.add("junji.cl")
        with patch.object(p, "_crawl_by_sections"):
            p._crawl_site("https://otro.cl")
        self.assertEqual(p._site_hosts, set())


class TestSeedClosed(unittest.TestCase):
    def _run(self, result=None, robots=True):
        p = _pipe()
        p.robots = MagicMock(can_fetch=MagicMock(return_value=robots))
        p.throttle = MagicMock(wait_turn=MagicMock(return_value=True))
        p.fetcher = MagicMock(fetch=MagicMock(return_value=result))
        p._crawl_site("https://www.ejemplo.cl")
        return p.notes

    def test_robots_on_the_seed_is_recorded(self):
        notes = self._run(robots=False)
        self.assertEqual(notes[0][1], "robots")

    def test_network_failure_on_the_seed_is_recorded(self):
        r = FetchResult("https://www.ejemplo.cl", "https://www.ejemplo.cl", None,
                        block=BlockCheck(True, "network", "Name or service not known"))
        notes = self._run(r)
        self.assertEqual(notes[0][1], "blocked")
        self.assertIn("Name or service", notes[0][2])

    def test_http_error_on_the_seed_is_recorded(self):
        r = FetchResult("https://www.ejemplo.cl", "https://www.ejemplo.cl", 404,
                        content_type="text/html", html="<html>no</html>")
        self.assertEqual(self._run(r)[0][1], "http_error")


class TestNoSections(unittest.TestCase):
    def test_falls_back_to_the_classic_crawl(self):
        p = _pipe()
        r = FetchResult("https://www.ejemplo.cl", "https://www.ejemplo.cl", 200,
                        content_type="text/html",
                        html="<html><body><p>Portada sin menú.</p></body></html>")
        p.robots = MagicMock(can_fetch=MagicMock(return_value=True))
        p.throttle = MagicMock(wait_turn=MagicMock(return_value=True))
        p.fetcher = MagicMock(fetch=MagicMock(return_value=r))
        with patch.object(p, "_crawl_classic") as classic:
            p._crawl_site("https://www.ejemplo.cl")
        classic.assert_called_once_with("https://www.ejemplo.cl")


if __name__ == "__main__":
    unittest.main()
