"""Un fallo del navegador degrada la página, no tumba el crawl del sitio."""

import unittest
from unittest.mock import MagicMock, patch

from webharvest.fetchers.fetchers import AdaptiveFetcher, FetchResult

# Config mínima con la forma real del YAML de producción
CFG = {
    "identity": {"user_agent": "t"},
    "fetching": {
        "timeout_seconds": 5,
        "max_retries": 1,
        "dynamic_detection": {"min_text_length": 400,
                              "spa_markers": ['id="app"', 'id="root"']},
        "playwright": {"headless": True, "wait_until": "networkidle",
                       "extra_wait_ms": 10, "scroll_passes": 0,
                       "pdf_click_probe": False},
    },
    "extraction": {"pdf": {"max_size_mb": 40}},
}
# cascarón: poco texto -> obliga a escalar a navegador
SHELL = '<html><body><div id="app"></div><p>Requires JavaScript</p></body></html>'
URL = "https://e.org/lista"


def _fetcher():
    with patch("webharvest.fetchers.fetchers.StaticFetcher") as S, \
         patch("webharvest.fetchers.fetchers.DynamicFetcher") as D:
        f = AdaptiveFetcher(CFG)
    f.static, f.dynamic = MagicMock(), MagicMock()
    f.static.fetch.return_value = FetchResult(
        url=URL, final_url=URL, status_code=200, html=SHELL, method="httpx")
    return f


class TestBrowserFailureFallback(unittest.TestCase):
    def test_launch_error_falls_back_to_static_html(self):
        f = _fetcher()
        # el error real observado en el cluster: /tmp sin cuota
        f.dynamic.fetch.side_effect = Exception(
            "BrowserType.launch: Unknown system error -122, mkdtemp '/tmp/x'")
        res = f.fetch(URL)
        self.assertEqual(res.method, "httpx")
        self.assertEqual(res.html, SHELL)      # devuelve lo estático, no falla

    def test_successful_render_wins(self):
        f = _fetcher()
        rendered = "<html><body><article>" + "palabra " * 200 + "</article></body></html>"
        f.dynamic.fetch.return_value = FetchResult(
            url=URL, final_url=URL, status_code=200, html=rendered,
            method="playwright")
        self.assertEqual(f.fetch(URL).method, "playwright")


if __name__ == "__main__":
    unittest.main()
