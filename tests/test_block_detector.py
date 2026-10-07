"""Detección de captchas: palabras completas y solo en páginas cortas."""

import unittest

from webharvest.compliance.policy import BlockDetector

ARTICLE = "<html><body><h1>Investigación</h1>" + "<p>" + " ".join(
    ["texto"] * 600) + " relaciones entre seres humanos y mega fauna.</p></body></html>"
ARTICLE_WITH_FORM = ARTICLE.replace("</body>",
    '<form><div class="g-recaptcha" data-sitekey="x"></div></form></body>')
CHALLENGE = ("<html><body><h1>Verificación de seguridad</h1>"
             "<p>Confirma que no eres un robot para continuar.</p>"
             '<div class="g-recaptcha"></div></body></html>')
CF = '<html><head><script src="/cdn-cgi/challenge-platform/h/b"></script></head><body>Checking...</body></html>'


class TestBlockDetector(unittest.TestCase):
    def test_substring_inside_a_word_is_not_a_marker(self):
        self.assertFalse(BlockDetector.check(200, ARTICLE).is_blocked)

    def test_long_page_with_recaptcha_widget_is_not_a_wall(self):
        self.assertFalse(BlockDetector.check(200, ARTICLE_WITH_FORM).is_blocked)

    def test_short_challenge_page_is_blocked(self):
        b = BlockDetector.check(200, CHALLENGE)
        self.assertTrue(b.is_blocked); self.assertEqual(b.kind, "captcha")

    def test_technical_marker_on_short_page(self):
        self.assertTrue(BlockDetector.check(200, CF).is_blocked)

    def test_http_block_status(self):
        b = BlockDetector.check(403, None)
        self.assertEqual((b.is_blocked, b.kind), (True, "http_block"))


if __name__ == "__main__":
    unittest.main()
