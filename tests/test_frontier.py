"""Tests del orden de prioridad de la Frontier (5 niveles)."""

import unittest

from webharvest.discovery.crawler import CrawlTarget, Frontier, is_excluded, is_query_trap

CFG = {
    "max_depth": 3, "max_pages_per_site": 100, "follow_subdomains": False,
    "content_hints": ["/news"], "exclude_patterns": [],
}
SEED = "https://example.org/"


class TestFrontierTiers(unittest.TestCase):
    def test_priority_order_docs_items_hints_sitemap_bfs(self):
        f = Frontier(CFG)
        f.add(CrawlTarget("https://example.org/about"), SEED)                       # normal
        f.add(CrawlTarget("https://example.org/x", from_sitemap=True), SEED)        # priority
        f.add(CrawlTarget("https://example.org/news/cat/"), SEED)                   # high (hint)
        f.add(CrawlTarget("https://example.org/news/post-1/", from_listing=True), SEED)  # items
        f.add(CrawlTarget("https://example.org/f.pdf", hint_pdf=True), SEED)        # docs
        order = [f.next().url for _ in range(5)]
        self.assertEqual(order, [
            "https://example.org/f.pdf",
            "https://example.org/news/post-1/",
            "https://example.org/news/cat/",
            "https://example.org/x",
            "https://example.org/about",
        ])

    def test_dedup_and_returns_bool(self):
        f = Frontier(CFG)
        self.assertTrue(f.add(CrawlTarget("https://example.org/a"), SEED))
        self.assertFalse(f.add(CrawlTarget("https://example.org/a"), SEED))

    def test_cross_domain_only_for_documents(self):
        f = Frontier(CFG)
        self.assertFalse(f.add(CrawlTarget("https://otro.org/pagina"), SEED))
        self.assertTrue(f.add(CrawlTarget("https://otro.org/doc.pdf", hint_pdf=True), SEED))


if __name__ == "__main__":
    unittest.main()


class TestExcludePatterns(unittest.TestCase):
    EX = ["/cart", "/login", "/tag/", ".jpg", "?share="]

    def test_path_pattern_matches_whole_segment_only(self):
        self.assertTrue(is_excluded("https://e.org/cart", self.EX))
        self.assertTrue(is_excluded("https://e.org/cart/", self.EX))
        self.assertTrue(is_excluded("https://e.org/tienda/cart?x=1", self.EX))
        self.assertFalse(is_excluded("https://e.org/cartelera", self.EX))
        self.assertFalse(is_excluded("https://e.org/cartelera/concierto", self.EX))
        self.assertFalse(is_excluded("https://e.org/loginformacion", self.EX))

    def test_substring_patterns_unchanged(self):
        self.assertTrue(is_excluded("https://e.org/tag/x", self.EX))
        self.assertTrue(is_excluded("https://e.org/a/foto.JPG", self.EX))
        self.assertTrue(is_excluded("https://e.org/p?share=fb", self.EX))
        self.assertFalse(is_excluded("https://e.org/noticias/1", self.EX))


class TestQueryTrap(unittest.TestCase):
    def test_nested_or_repeated_query_is_a_trap(self):
        self.assertTrue(is_query_trap("https://e.org/p?current=pdf?current=pdf"))
        self.assertTrue(is_query_trap("https://e.org/p?current=pdf%3Fcurrent%3Dpdf"))
        self.assertTrue(is_query_trap("https://e.org/p?page=1&page=2"))
        self.assertFalse(is_query_trap("https://e.org/p?current=pdf"))
        self.assertFalse(is_query_trap("https://e.org/noticias?page=3"))
        self.assertFalse(is_query_trap("https://e.org/p"))

    def test_frontier_rejects_trap_with_reason(self):
        rejected = []
        f = Frontier(CFG, on_reject=lambda u, w: rejected.append(w))
        f.add(CrawlTarget("https://example.org/p?x=1?x=1"), SEED)
        self.assertEqual(rejected, ["trap"])
