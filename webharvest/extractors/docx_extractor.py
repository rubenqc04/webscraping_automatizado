"""Extracción de documentos Word (.docx / .doc).

- `.docx` (Office Open XML): `mammoth` lo convierte a HTML semántico
  (títulos, listas, tablas) y de ahí `markdownify` produce Markdown.
  Fallback: `python-docx`-menos, se leen los XML internos directamente.
- `.doc` (Word 97-2003, binario): no hay extractor puro-Python fiable;
  se guarda el binario con `extraction_status: raw_only` para procesarlo
  después (LibreOffice headless, antiword, o un servicio de conversión).

La metadata nativa sale de `docProps/core.xml` (Dublin Core: título,
autor, fechas, keywords) cuando existe.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Optional
from xml.etree import ElementTree as ET

import mammoth
from markdownify import markdownify as md

from ..models import (Document, DocumentMetadata, SourceType,
                      count_words, make_doc_id)

log = logging.getLogger("webharvest.extract.docx")

_CORE_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}


def is_docx(data: bytes) -> bool:
    """OOXML = ZIP con word/document.xml."""
    if data[:4] != b"PK\x03\x04":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "word/document.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def is_legacy_doc(data: bytes) -> bool:
    """Word 97-2003: contenedor OLE2."""
    return data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _core_properties(data: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "docProps/core.xml" not in z.namelist():
                return {}
            root = ET.fromstring(z.read("docProps/core.xml"))
    except Exception:
        return {}

    def get(path: str) -> Optional[str]:
        el = root.find(path, _CORE_NS)
        return el.text.strip() if el is not None and el.text else None

    return {
        "title": get("dc:title"),
        "author": get("dc:creator"),
        "description": get("dc:description"),
        "keywords": get("cp:keywords"),
        "created": get("dcterms:created"),
        "language": get("dc:language"),
    }


def extract_docx_document(url: str, data: bytes, min_words: int = 20) -> Document:
    """Word -> Document. Los .doc legados quedan solo como binario."""
    doc_id = make_doc_id(url)
    legacy = is_legacy_doc(data)
    props = {} if legacy else _core_properties(data)

    metadata = DocumentMetadata(
        doc_id=doc_id,
        url=url,
        title=props.get("title"),
        authors=[props["author"]] if props.get("author") else [],
        description=props.get("description"),
        keywords=[k.strip() for k in (props.get("keywords") or "").split(",") if k.strip()],
        published_date=props.get("created"),
        language=props.get("language"),
        source_type=SourceType.DOCX.value,
    )

    if legacy:
        log.info("Word 97-2003 (.doc) sin extractor: se guarda solo el binario %s", url)
        metadata.extra["extraction_status"] = "raw_only"
        metadata.extra["reason"] = "formato .doc legado; convertir con LibreOffice/antiword"
        return Document(metadata=metadata, markdown="", raw_bytes=data, raw_ext=".doc")

    body_md = ""
    try:
        result = mammoth.convert_to_html(io.BytesIO(data))
        body_md = md(result.value, heading_style="ATX", strip=["img"]).strip()
    except Exception as exc:
        log.warning("mammoth falló en %s (%s); fallback a texto plano del XML", url, exc)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            body_md = " ".join(re.sub(r"<[^>]+>", " ", xml).split())
        except Exception:
            log.exception("Extracción de %s imposible; se guarda solo el binario", url)

    metadata.word_count = count_words(body_md)
    if metadata.word_count < min_words:
        metadata.extra["extraction_status"] = "raw_only"
        body_md = ""

    front = (
        f"---\n"
        f"doc_id: {doc_id}\n"
        f"title: {metadata.title or 'Sin título'}\n"
        f"url: {url}\n"
        f"source: docx\n"
        f"---\n\n"
    )
    return Document(
        metadata=metadata,
        markdown=(front + body_md + "\n") if body_md else "",
        raw_bytes=data,
        raw_ext=".docx",
    )
