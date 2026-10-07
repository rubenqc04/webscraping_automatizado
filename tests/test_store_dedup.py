"""Tests de deduplicación por contenido y alias de URLs en el store."""

import json
import tempfile
import unittest
from pathlib import Path

from webharvest.models import Document, DocumentMetadata
from webharvest.storage.store import DocumentStore

BODY = "Contenido sustantivo del documento repetido en dos rutas. " * 12


def make_store(tmp: Path) -> DocumentStore:
    return DocumentStore({"base_dir": str(tmp), "markdown_dir": "md",
                          "pdf_dir": "pdf", "metadata_dir": "meta",
                          "ocr_pending_file": "ocr/q.json",
                          "master_index": "meta/index.json"})


def make_doc(doc_id: str, url: str, body: str = BODY) -> Document:
    md = f"---\ndoc_id: {doc_id}\nurl: {url}\n---\n\n{body}"
    return Document(metadata=DocumentMetadata(doc_id=doc_id, url=url,
                                              title=f"t-{doc_id}"),
                    markdown=md)


class TestContentDedup(unittest.TestCase):
    def test_identical_body_is_not_saved_twice(self):
        tmp = Path(tempfile.mkdtemp())
        store = make_store(tmp)
        store.save(make_doc("aaa", "https://x.cl/noticias"))
        store.save(make_doc("bbb", "https://x.cl/noticias/165"))
        self.assertEqual(store.stats()["total_documents"], 1)
        # la segunda URL queda como alias del original
        meta = json.loads((tmp / "meta/aaa.json").read_text())
        self.assertEqual(meta["extra"]["duplicate_urls"], ["https://x.cl/noticias/165"])
        self.assertFalse((tmp / "md/bbb.md").exists())

    def test_different_body_saved_normally(self):
        tmp = Path(tempfile.mkdtemp())
        store = make_store(tmp)
        store.save(make_doc("aaa", "https://x.cl/a"))
        store.save(make_doc("bbb", "https://x.cl/b", body="Otro texto distinto. " * 15))
        self.assertEqual(store.stats()["total_documents"], 2)

    def test_short_texts_not_deduped(self):
        tmp = Path(tempfile.mkdtemp())
        store = make_store(tmp)
        store.save(make_doc("aaa", "https://x.cl/a", body="corto"))
        store.save(make_doc("bbb", "https://x.cl/b", body="corto"))
        self.assertEqual(store.stats()["total_documents"], 2)

    def test_dedup_survives_store_reload(self):
        tmp = Path(tempfile.mkdtemp())
        make_store(tmp).save(make_doc("aaa", "https://x.cl/a"))
        store2 = make_store(tmp)          # relee índice del disco
        store2.save(make_doc("bbb", "https://x.cl/b"))
        self.assertEqual(store2.stats()["total_documents"], 1)


if __name__ == "__main__":
    unittest.main()
