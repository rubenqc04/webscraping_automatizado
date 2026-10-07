"""Tests del extractor de Word con un .docx mínimo construido en memoria."""

import io
import unittest
import zipfile

from webharvest.extractors.docx_extractor import (extract_docx_document,
                                                  is_docx, is_legacy_doc)

CONTENT_TYPES = """<?xml version="1.0"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml"
    ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""

DOCUMENT = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Informe de prueba</w:t></w:r></w:p>
    <w:p><w:r><w:t>Este es el contenido principal del documento de Word,
    con palabras suficientes para superar el umbral de extraccion minima
    del extractor y validar la conversion a Markdown estructurado.</w:t></w:r></w:p>
  </w:body>
</w:document>"""

CORE = """<?xml version="1.0"?>
<cp:coreProperties
  xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Informe de prueba</dc:title>
  <dc:creator>Equipo WebHarvest</dc:creator>
  <cp:keywords>prueba, docx</cp:keywords>
  <dcterms:created xsi:type="dcterms:W3CDTF">2026-01-15T00:00:00Z</dcterms:created>
</cp:coreProperties>"""


def build_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", DOCUMENT)
        z.writestr("docProps/core.xml", CORE)
    return buf.getvalue()


class TestDocxExtractor(unittest.TestCase):
    def test_detection(self):
        data = build_docx()
        self.assertTrue(is_docx(data))
        self.assertFalse(is_legacy_doc(data))
        self.assertTrue(is_legacy_doc(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100))

    def test_extraction_markdown_and_metadata(self):
        doc = extract_docx_document("https://example.org/informe.docx", build_docx())
        self.assertEqual(doc.metadata.source_type, "docx")
        self.assertEqual(doc.metadata.title, "Informe de prueba")
        self.assertEqual(doc.metadata.authors, ["Equipo WebHarvest"])
        self.assertIn("contenido principal", doc.markdown)
        self.assertEqual(doc.raw_ext, ".docx")
        self.assertGreater(doc.metadata.word_count, 20)

    def test_legacy_doc_stored_raw(self):
        data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 256
        doc = extract_docx_document("https://example.org/viejo.doc", data)
        self.assertEqual(doc.markdown, "")
        self.assertEqual(doc.raw_ext, ".doc")
        self.assertEqual(doc.metadata.extra["extraction_status"], "raw_only")


if __name__ == "__main__":
    unittest.main()
