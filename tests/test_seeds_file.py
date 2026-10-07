"""Tests de --url-file (semillas desde CSV de search_links o txt plano)."""

import tempfile
import unittest
from pathlib import Path

from main import load_seeds_file


class TestSeedsFile(unittest.TestCase):
    def test_csv_with_url_header_and_context(self):
        p = Path(tempfile.mkstemp(suffix=".csv")[1])
        p.write_text(
            'Consulta_Origen,Titulo,URL\n'
            '"site:.pe ""quechua""",Un título,https://ejemplo.pe/articulo\n'
            'otra consulta,Otro,https://ejemplo.bo/doc\n'
            'repetida,Dup,https://ejemplo.pe/articulo\n',   # dedupe
            encoding="utf-8")
        seeds, ctx = load_seeds_file(p)
        self.assertEqual(seeds, ["https://ejemplo.pe/articulo", "https://ejemplo.bo/doc"])
        self.assertEqual(ctx["https://ejemplo.pe/articulo"]["Consulta_Origen"],
                         'site:.pe "quechua"')
        self.assertEqual(ctx["https://ejemplo.pe/articulo"]["Titulo"], "Un título")

    def test_csv_without_recognizable_header(self):
        p = Path(tempfile.mkstemp(suffix=".csv")[1])
        p.write_text("https://a.cl/x,algo\nhttps://b.cl/y,otro\n", encoding="utf-8")
        seeds, ctx = load_seeds_file(p)
        self.assertEqual(seeds, ["https://a.cl/x", "https://b.cl/y"])

    def test_txt_with_comments(self):
        p = Path(tempfile.mkstemp(suffix=".txt")[1])
        p.write_text("# comentario\nhttps://a.cl/x\n\nhttps://b.cl/y\n", encoding="utf-8")
        seeds, ctx = load_seeds_file(p)
        self.assertEqual(seeds, ["https://a.cl/x", "https://b.cl/y"])
        self.assertEqual(ctx, {})


if __name__ == "__main__":
    unittest.main()
