"""Tests del exportador de dataset (particiones por derechos, JSONL)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts/export_dataset.py"


def build_corpus(base: Path) -> None:
    (base / "metadata").mkdir(parents=True)
    (base / "markdown").mkdir()
    docs = [
        ("d1", "https://x.gob.cl/a", "sector"),      # -> permisivo
        ("d2", "https://cc.org/b", "cc"),            # -> permisivo
        ("d3", "https://news.cl/c", "reservados"),   # -> derechos_reservados
        ("d4", "https://ai.cl/d", "ia"),             # -> reservado_ia
        ("d5", "https://gris.cl/e", "gris"),         # -> revisar
    ]
    index = {}
    for doc_id, url, _ in docs:
        body = "Palabra de contenido sustantivo para el dataset. " * 20
        (base / f"markdown/{doc_id}.md").write_text(
            f"---\ndoc_id: {doc_id}\n---\n\n{body}", encoding="utf-8")
        (base / f"metadata/{doc_id}.json").write_text(json.dumps({
            "doc_id": doc_id, "url": url, "title": f"T-{doc_id}",
            "markdown_file": f"markdown/{doc_id}.md",
            "word_count": len(body.split()), "language": "es",
            "crawl_path": [{"url": url, "kind": "seed"}], "depth": 0,
            "extra": {"seed_context": {"Institucion": "Inst"}},
        }), encoding="utf-8")
        index[doc_id] = {"url": url}
    # doc sin markdown (PDF pendiente de OCR): debe descartarse
    index["d6"] = {"url": "https://x.gob.cl/scan.pdf"}
    (base / "metadata/d6.json").write_text(json.dumps({
        "doc_id": "d6", "url": "https://x.gob.cl/scan.pdf"}), encoding="utf-8")
    (base / "metadata/index.json").write_text(json.dumps(index), encoding="utf-8")

    (base / "rights_report.json").write_text(json.dumps({"dominios": [
        {"domain": "x.gob.cl", "clasificacion_preliminar": "sector-publico (revisar)"},
        {"domain": "cc.org", "clasificacion_preliminar": "cc-license"},
        {"domain": "news.cl", "clasificacion_preliminar": "todos-los-derechos-reservados"},
        {"domain": "ai.cl", "clasificacion_preliminar": "tdm-o-ai-reservado (bloquea bots)"},
        {"domain": "gris.cl", "clasificacion_preliminar": "sin-declaracion-visible"},
    ]}), encoding="utf-8")


def run(base: Path, *extra: str):
    return subprocess.run([sys.executable, str(SCRIPT), str(base), *extra],
                          capture_output=True, text=True)


class TestExport(unittest.TestCase):
    def test_partitions_by_rights(self):
        base = Path(tempfile.mkdtemp())
        build_corpus(base)
        out = run(base)
        self.assertEqual(out.returncode, 0, out.stderr)
        ds = base / "dataset"
        lines = lambda p: (ds / p).read_text().strip().splitlines()
        self.assertEqual(len(lines("permisivo.jsonl")), 2)          # gob + cc
        self.assertEqual(len(lines("derechos_reservados.jsonl")), 1)
        self.assertEqual(len(lines("reservado_ia.jsonl")), 1)
        self.assertEqual(len(lines("revisar.jsonl")), 1)

    def test_record_carries_text_provenance_and_rights(self):
        base = Path(tempfile.mkdtemp())
        build_corpus(base)
        run(base)
        rec = json.loads((base / "dataset/permisivo.jsonl").read_text()
                         .strip().splitlines()[0])
        self.assertIn("contenido sustantivo", rec["text"])
        self.assertNotIn("doc_id:", rec["text"])            # front-matter fuera
        self.assertEqual(rec["provenance"]["seed_context"]["Institucion"], "Inst")
        self.assertIn(rec["rights"]["partition"], ("permisivo",))

    def test_only_filter_and_manifest(self):
        base = Path(tempfile.mkdtemp())
        build_corpus(base)
        run(base, "--only", "permisivo")
        ds = base / "dataset"
        self.assertTrue((ds / "permisivo.jsonl").exists())
        manifest = json.loads((ds / "MANIFEST.json").read_text())
        self.assertEqual(manifest["totales"]["documentos"], 2)
        self.assertTrue(any(k.startswith("fuera_de_--only")
                            for k in manifest["descartados"]))

    def test_docs_without_text_are_skipped(self):
        base = Path(tempfile.mkdtemp())
        build_corpus(base)
        run(base)
        manifest = json.loads((base / "dataset/MANIFEST.json").read_text())
        self.assertEqual(manifest["descartados"].get("sin_texto"), 1)


if __name__ == "__main__":
    unittest.main()
