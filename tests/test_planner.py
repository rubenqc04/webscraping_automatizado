"""Tests del planificador agéntico y la terminación por saturación."""

import unittest

from webharvest.planner import decide_plan


class TestDecidePlan(unittest.TestCase):
    def test_listing_seed(self):
        p = decide_plan("listing", listing_items=25, sections_found=0,
                        sitemap_urls=0)
        self.assertEqual(p.strategy, "listing")

    def test_article_seed_goes_directed(self):
        p = decide_plan("article", 0, 0, 0, fulltext_links=2)
        self.assertEqual(p.strategy, "directed")
        self.assertFalse(p.overrides["follow_article_links"])

    def test_portal_with_sections(self):
        p = decide_plan("navigation", 0, sections_found=5, sitemap_urls=0)
        self.assertEqual(p.strategy, "sections")
        self.assertEqual(p.overrides["mode"], "sections")

    def test_opaque_portal_with_sitemap(self):
        # el caso DSpace: home sin secciones útiles pero sitemap poblado
        p = decide_plan("navigation", 0, sections_found=1, sitemap_urls=50)
        self.assertEqual(p.strategy, "sitemap")
        self.assertTrue(p.overrides["use_sitemaps"])

    def test_fallback_bfs(self):
        p = decide_plan("navigation", 0, 1, sitemap_urls=2)
        self.assertEqual(p.strategy, "bfs")

    def test_plan_records_signals_and_reasons(self):
        p = decide_plan("listing", 12, 0, 0)
        self.assertEqual(p.signals["listing_items"], 12)
        self.assertTrue(p.reasons and p.to_dict()["strategy"] == "listing")


class TestSaturation(unittest.TestCase):
    def make(self):
        from tests.test_provenance import make_pipeline
        return make_pipeline()

    def test_no_yield_saturates_after_window(self):
        p = self.make()
        p.saturation_window = 10
        p._reset_site_counters()
        p._site_fetches = 10          # 10 páginas, ni un documento
        self.assertIsNotNone(p._saturated())

    def test_yield_resets_the_clock(self):
        p = self.make()
        p.saturation_window = 10
        p._reset_site_counters()
        p._site_fetches = 9
        p._last_yield_fetch = p._site_fetches   # doc guardado en la página 9
        p._site_fetches = 18
        self.assertIsNone(p._saturated())       # solo 9 sin rendir
        p._site_fetches = 19
        self.assertIsNotNone(p._saturated())

    def test_exhaustive_lifts_listing_cap(self):
        from tests.test_provenance import make_pipeline
        import tests.test_provenance as tp
        p = tp.make_pipeline()
        self.assertEqual(p.max_listing_pages, 3)     # default
        # con exhaustive el tope de listados desaparece
        cfg = dict(p.cfg); cfg["discovery"] = {**cfg["discovery"], "exhaustive": True}
        from webharvest.pipeline import ScrapePipeline
        p2 = ScrapePipeline(cfg, store=p.store, throttle=p.throttle, robots=p.robots)
        try:
            self.assertGreater(p2.max_listing_pages, 10 ** 5)
        finally:
            p2.close()
        p.close()


if __name__ == "__main__":
    unittest.main()


class TestLLMArbiter(unittest.TestCase):
    """El árbitro solo actúa en planes dudosos y nunca por debajo de la heurística."""

    class FakeLLM:
        def __init__(self, answer): self.answer = answer; self.calls = 0
        def ask_json(self, system, user): self.calls += 1; return self.answer

    def test_confident_plan_never_calls_llm(self):
        from webharvest.planner import refine_with_llm
        plan = decide_plan("listing", 25, 0, 0)          # confiado
        llm = self.FakeLLM({"strategy": "bfs", "reason": "x"})
        out = refine_with_llm(plan, llm, "t", [], [])
        self.assertEqual(out.strategy, "listing")
        self.assertEqual(llm.calls, 0)                   # no se consultó

    def test_ambiguous_plan_uses_llm_verdict(self):
        from webharvest.planner import refine_with_llm
        plan = decide_plan("navigation", 0, 1, 2)        # dudoso -> bfs
        self.assertTrue(plan.low_confidence)
        llm = self.FakeLLM({"strategy": "sections", "reason": "tiene menú temático"})
        out = refine_with_llm(plan, llm, "Portal", ["Inicio"], ["Salud", "Educación"])
        self.assertEqual(out.strategy, "sections")
        self.assertEqual(out.decided_by, "llm")
        self.assertEqual(out.overrides["mode"], "sections")

    def test_invalid_llm_answer_falls_back(self):
        from webharvest.planner import refine_with_llm
        plan = decide_plan("navigation", 0, 1, 2)
        for bad in ({"strategy": "inventada"}, {}, None):
            out = refine_with_llm(plan, self.FakeLLM(bad), "t", [], [])
            self.assertEqual(out.strategy, "bfs")         # se mantiene la heurística
            self.assertEqual(out.decided_by, "heuristic")

    def test_no_llm_configured_keeps_heuristic(self):
        from webharvest.planner import refine_with_llm
        plan = decide_plan("navigation", 0, 1, 2)
        out = refine_with_llm(plan, None, "t", [], [])
        self.assertEqual(out.strategy, "bfs")


class TestLLMClient(unittest.TestCase):
    def test_unavailable_server_returns_none(self):
        from webharvest.llm import LLMClient
        c = LLMClient("http://127.0.0.1:59999", "m", timeout=1)   # puerto muerto
        self.assertFalse(c.available())
        self.assertIsNone(c.ask_json("s", "u"))
        c.close()

    def test_extract_json_tolerates_prose_and_fences(self):
        from webharvest.llm import _extract_json
        self.assertEqual(_extract_json('```json\n{"strategy":"listing"}\n```'),
                         {"strategy": "listing"})
        self.assertEqual(_extract_json('Creo que: {"strategy":"bfs"} listo'),
                         {"strategy": "bfs"})
        self.assertIsNone(_extract_json("sin json aquí"))
