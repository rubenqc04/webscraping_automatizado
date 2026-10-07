"""Libro de visitas + medición de cobertura."""

import json
import tempfile
import unittest
from pathlib import Path

from webharvest.analysis.coverage import (compare, funnel, ledger_by_url,
                                          normalize, parse_wget_log)
from webharvest.discovery.crawler import CrawlTarget, Frontier
from webharvest.storage.ledger import VisitLedger, read_ledger

CFG = {"max_depth": 2, "max_pages_per_site": 2, "follow_subdomains": False,
       "content_hints": ["/news"], "exclude_patterns": ["/tag/"]}
SEED = "https://example.org/"


class TestFrontierLedgerHooks(unittest.TestCase):
    def test_rejections_are_reported_once_with_reason(self):
        rejected = []
        f = Frontier(CFG, on_reject=lambda u, w: rejected.append((u, w)))
        f.add(CrawlTarget("https://otro.org/p"), SEED)
        f.add(CrawlTarget("https://otro.org/p"), SEED)          # duplicado: silencio
        f.add(CrawlTarget("https://example.org/tag/x"), SEED)
        f.add(CrawlTarget("https://example.org/deep", depth=5), SEED)
        self.assertEqual(rejected, [("https://otro.org/p", "offsite"),
                                    ("https://example.org/tag/x", "excluded"),
                                    ("https://example.org/deep", "depth")])

    def test_drain_returns_what_budget_left_behind(self):
        f = Frontier(CFG)
        for i in range(5):
            f.add(CrawlTarget(f"https://example.org/p{i}"), SEED)
        self.assertIsNotNone(f.next()); self.assertIsNotNone(f.next())
        self.assertIsNone(f.next())                              # presupuesto 2
        left = [t.url for t in f.drain()]
        self.assertEqual(len(left), 3)
        self.assertEqual(len(f), 0)


class TestVisitLedger(unittest.TestCase):
    def test_roundtrip_and_vocabulary(self):
        with tempfile.TemporaryDirectory() as d:
            led = VisitLedger(Path(d) / "_ledger" / "visits.jsonl")
            led.record("https://e.org/a", "enqueued", seed=SEED, kind="seed")
            led.record("https://e.org/a", "saved", seed=SEED)
            with self.assertRaises(ValueError):
                led.record("https://e.org/b", "inventado")
            led.close()
            rows = read_ledger(Path(d) / "_ledger" / "visits.jsonl")
        self.assertEqual([r["decision"] for r in rows], ["enqueued", "saved"])
        self.assertEqual(rows[0]["kind"], "seed")


class TestWgetLog(unittest.TestCase):
    LOG = """2026-09-04 00:01:00 URL:https://www.e.org/ [31844/31844] -> "www.e.org/index.html" [1]
unlink: No such file or directory
2026-09-04 00:01:02 URL:https://www.e.org/doc.pdf 200 OK
2026-09-04 00:01:03 URL:https://www.e.org/roto 404 Not Found
Remote file does not exist -- broken link!!!
2026-09-04 00:01:04 URL:https://otro.org/x 200 OK
2026-09-04 00:01:05 URL:https://www.e.org/noticias/a [26628/26628] -> "www.e.org/noticias/a" [1]
"""

    def test_size_bracket_without_total(self):
        """wget omite el total cuando el servidor no declara content-length."""
        log = ('2026-09-15 15:05:08 URL:https://demre.cl/ [35872] -> "x.tmp" [1]\n'
               '2026-09-15 15:05:11 URL:https://demre.cl/portales/a [16690] -> "y" [1]\n')
        self.assertEqual(parse_wget_log(log, "demre.cl"),
                         ["https://demre.cl/", "https://demre.cl/portales/a"])

    def test_reads_both_line_shapes_and_filters(self):
        self.assertEqual(parse_wget_log(self.LOG, "e.org"), [
            "https://www.e.org/", "https://www.e.org/doc.pdf",
            "https://www.e.org/noticias/a"])


class TestCoverage(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("HTTPS://WWW.E.org/a/?utm_source=x#frag"),
                         "https://e.org/a")
        self.assertEqual(normalize("https://e.org/a/index.html"), "https://e.org/a")
        self.assertEqual(normalize("https://e.org/a?b=1&a=2"),
                         normalize("https://e.org/a?a=2&b=1"))

    def test_formats_without_an_extractor_are_excluded(self):
        from webharvest.analysis.coverage import is_extractable, compare, ledger_by_url
        for u in ("https://e.org/datos.xlsx", "https://e.org/a.csv",
                  "https://e.org/mapa.kmz", "https://e.org/x.zip"):
            self.assertFalse(is_extractable(u), u)
        for u in ("https://e.org/informe.pdf", "https://e.org/acta.docx",
                  "https://e.org/nota"):
            self.assertTrue(is_extractable(u), u)
        # no deben contar en el denominador del recall
        inv = {normalize(u): "s" for u in ["https://e.org/n/1",
                                           "https://e.org/datos.xlsx",
                                           "https://e.org/serie.csv"]}
        c = compare(inv, ledger_by_url([
            {"url": "https://e.org/n/1", "decision": "enqueued"},
            {"url": "https://e.org/n/1", "decision": "saved"}], host="e.org"))
        self.assertEqual(c["inventory_content"], 1)
        self.assertEqual(c["inventory_no_extraible"], 2)
        self.assertEqual(c["recall_pct"], 100.0)

    def test_pagination_and_utility_pages_are_not_content(self):
        from webharvest.analysis.coverage import is_utility
        for u in ("https://e.org/noticias?page=3", "https://e.org/blog/page/2",
                  "https://e.org/wp-login.php", "https://e.org/tag/x", "https://e.org/a.jpg"):
            self.assertTrue(is_utility(u), u)
        for u in ("https://e.org/noticias/una-nota", "https://e.org/doc.pdf",
                  "https://e.org/pagelayout/intro"):
            self.assertFalse(is_utility(u), u)

    def _ledger(self):
        return [
            {"url": "https://e.org/", "decision": "enqueued", "kind": "seed"},
            {"url": "https://e.org/", "decision": "listing", "reason": "3 ítems"},
            {"url": "https://e.org/n/1", "decision": "enqueued", "kind": "listing_item"},
            {"url": "https://e.org/n/1", "decision": "saved"},
            {"url": "https://e.org/n/2", "decision": "enqueued", "kind": "listing_item"},
            {"url": "https://e.org/n/2", "decision": "hub", "reason": "link_density 0.9"},
            {"url": "https://e.org/n/3", "decision": "enqueued"},
            {"url": "https://e.org/n/3", "decision": "not_fetched", "reason": "presupuesto"},
            {"url": "https://e.org/n/5", "decision": "enqueued"},
            {"url": "https://e.org/n/5", "decision": "saved"},
            {"url": "https://x.org/z", "decision": "rejected", "reason": "offsite"},
        ]

    def test_funnel(self):
        f = funnel(ledger_by_url(self._ledger(), host="e.org"))
        self.assertEqual(f["urls_seen"], 5)
        self.assertEqual(f["fetched"], 4)
        self.assertEqual(f["saved"], 2)
        self.assertEqual(f["by_decision"]["not_fetched"], 1)

    def test_compare_stages_and_metrics(self):
        inv = {normalize(u): "sitemap" for u in [
            "https://e.org/n/1", "https://e.org/n/2", "https://e.org/n/3",
            "https://e.org/n/4", "https://e.org/wp-login.php", "https://e.org/tag/t"]}
        c = compare(inv, ledger_by_url(self._ledger(), host="e.org"))
        self.assertEqual(c["inventory_content"], 4)
        self.assertEqual(c["inventory_utility"], 2)
        self.assertEqual(c["recall_pct"], 25.0)      # solo n/1
        self.assertEqual(c["reach_pct"], 75.0)       # n/1 n/2 n/3
        self.assertEqual(c["fetch_pct"], 50.0)       # n/1 n/2
        stages = {m["url"].rsplit("/", 1)[1]: m["stage"] for m in c["missing"]}
        self.assertEqual(stages, {"2": "descartada", "3": "sin_descargar", "4": "no_vista"})
        # n/5 se guardó pero el inventario no lo tenía: extra, no error
        self.assertEqual(c["saved_outside_inventory"], 1)
        self.assertEqual(c["precision_pct"], 50.0)


if __name__ == "__main__":
    unittest.main()
