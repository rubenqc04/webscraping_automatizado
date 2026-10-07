"""Modelos de datos centrales.

Cada pieza de contenido extraída se convierte en un `Document` con un
identificador determinístico (hash de la URL canónica). Ese mismo ID
enlaza:
  - metadata/<doc_id>.json      -> metadata estructurada
  - markdown/<doc_id>.md        -> texto en Markdown
  - pdfs/<doc_id>.pdf           -> binario original (si aplica)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


_CJK = None      # rango CJK+kana+hangul, compilado perezosamente


def count_words(text: str) -> int:
    """Palabras de un texto, válido también para idiomas sin espacios.

    En CJK (chino/japonés/coreano) `split()` subestima brutalmente
    (98KB de japonés ~ 1.200 "palabras"); la convención estándar es
    contar cada carácter CJK como una palabra.
    """
    global _CJK
    if _CJK is None:
        import re
        _CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
    cjk_chars = len(_CJK.findall(text))
    non_cjk_tokens = len(_CJK.sub(" ", text).split())
    return cjk_chars + non_cjk_tokens


def make_doc_id(url: str) -> str:
    """ID determinístico y corto a partir de la URL canónica."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class SourceType(str, Enum):
    HTML_STATIC = "html_static"
    HTML_DYNAMIC = "html_dynamic"      # requirió render con navegador
    PDF = "pdf"
    DOCX = "docx"                      # Word (.docx / .doc)


class ContentCategory(str, Enum):
    NEWS = "news"
    SCIENTIFIC = "scientific"
    BLOG = "blog"
    GOVERNMENT = "government"
    GENERIC = "generic"


class OcrStatus(str, Enum):
    NOT_NEEDED = "not_needed"
    PENDING = "pending"            # detectado como escaneado; en cola para estrategia OCR
    DONE = "done"


@dataclass
class DocumentMetadata:
    doc_id: str
    url: str
    canonical_url: Optional[str] = None
    title: Optional[str] = None
    authors: list[str] = field(default_factory=list)
    published_date: Optional[str] = None
    language: Optional[str] = None
    description: Optional[str] = None
    keywords: list[str] = field(default_factory=list)
    site_name: Optional[str] = None
    domain: Optional[str] = None
    category: str = ContentCategory.GENERIC.value
    source_type: str = SourceType.HTML_STATIC.value

    # Rutas relativas de los ficheros asociados (relación por doc_id)
    markdown_file: Optional[str] = None
    pdf_file: Optional[str] = None

    # Trazabilidad del crawl: cómo se llegó a este documento
    depth: Optional[int] = None          # saltos desde la semilla
    crawl_path: list = field(default_factory=list)
    # cadena [{url, kind, section?}, ...] desde la semilla hasta aquí;
    # kind: seed|section|sitemap|listing_item|pagination|pagination_probe|
    #       fulltext|document_link|content_link|bfs

    # Métricas y trazabilidad
    word_count: int = 0
    n_pages: Optional[int] = None            # PDFs
    ocr_status: str = OcrStatus.NOT_NEEDED.value
    ocr_signals: dict[str, Any] = field(default_factory=dict)
    fetch_method: Optional[str] = None       # httpx | playwright | download
    http_status: Optional[int] = None
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Document:
    metadata: DocumentMetadata
    markdown: str = ""             # cuerpo del texto en Markdown
    raw_bytes: Optional[bytes] = None  # binario original (PDF/DOCX/...)
    raw_ext: str = ".pdf"          # extensión del binario original
