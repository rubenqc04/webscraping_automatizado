"""Test del modo --fix-boilerplate del auditor (corpus sintético en tmp)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts/audit_corpus.py"
BOILER1 = "Suscríbete a nuestro boletín semanal de noticias del dominio"
BOILER2 = "Comparte este artículo en todas tus redes sociales favoritas"


def build_corpus(base: Path, n_docs: int = 6) -> None:
    (base / "metadata").mkdir(parents=True)
    (base / "markdown").mkdir()
    index = {}
    for i in range(n_docs):
        doc_id = f"doc{i:04d}{'0' * 12}"
        body = (f"Título del artículo {i}\n\n{BOILER1}\n\n"
                + f"Contenido real del artículo número {i} con palabras suficientes "
                  f"para no quedar vacío tras la limpieza del boilerplate. " * 5
                + f"\n\n{BOILER2}\n")
        md = f"---\ndoc_id: {doc_id}\n---\n\n{body}"
        (base / f"markdown/{doc_id}.md").write_text(md, encoding="utf-8")
        meta = {"doc_id": doc_id, "url": f"https://ejemplo.org/a{i}",
                "domain": "ejemplo.org", "title": f"Artículo {i}",
                "markdown_file": f"markdown/{doc_id}.md",
                "word_count": len(body.split()), "extra": {}}
        (base / f"metadata/{doc_id}.json").write_text(
            json.dumps(meta), encoding="utf-8")
        index[doc_id] = {"url": meta["url"], "title": meta["title"]}
    (base / "metadata/index.json").write_text(json.dumps(index), encoding="utf-8")


class TestFixBoilerplate(unittest.TestCase):
    def test_removes_common_lines_and_is_idempotent(self):
        base = Path(tempfile.mkdtemp())
        build_corpus(base)
        run = lambda *extra: subprocess.run(
            [sys.executable, str(SCRIPT), str(base), *extra],
            capture_output=True, text=True)

        out1 = run("--fix-boilerplate")
        self.assertEqual(out1.returncode, 0, out1.stderr)
        md = (base / "markdown/doc000000000000000000".replace("00000000000000000000", "doc0000000000000000")).with_name("doc0000000000000000.md")
        text = md.read_text(encoding="utf-8")
        self.assertNotIn(BOILER1, text)
        self.assertNotIn(BOILER2, text)
        self.assertIn("Contenido real del artículo", text)
        self.assertIn("doc_id: doc0000", text)          # front-matter intacto

        meta = json.loads((base / "metadata/doc0000000000000000.json").read_text())
        self.assertEqual(meta["extra"]["boilerplate_lines_removed"], 2)

        # segunda pasada: idempotente, no remueve nada más
        run("--fix-boilerplate")
        meta2 = json.loads((base / "metadata/doc0000000000000000.json").read_text())
        self.assertEqual(meta2["extra"]["boilerplate_lines_removed"], 2)

    def test_report_only_does_not_touch_files(self):
        base = Path(tempfile.mkdtemp())
        build_corpus(base)
        before = (base / "markdown/doc0000000000000000.md").read_text()
        subprocess.run([sys.executable, str(SCRIPT), str(base)],
                       capture_output=True, text=True)
        after = (base / "markdown/doc0000000000000000.md").read_text()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
