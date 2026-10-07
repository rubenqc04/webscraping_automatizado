"""Extracción de PDFs y triaje de OCR.

Flujo:
    1. Se analiza el PDF con PyMuPDF (fitz) para medir señales de
       "documento escaneado":
         - caracteres de texto extraíble por página (capa de texto)
         - proporción del área de página cubierta por imágenes
         - presencia de fuentes embebidas
    2. Si el PDF tiene capa de texto sana -> pymupdf4llm lo convierte
       a Markdown de alta calidad (títulos, tablas, listas).
    3. Si parece escaneado (o híbrido pobre) -> NO se hace OCR aquí.
       El documento se encola en `ocr_pending` con sus señales, para
       decidir después la estrategia (tesseract/ocrmypdf local, un
       modelo de visión, o un servicio externo) según volumen, idioma
       y presupuesto. Esto mantiene el crawl rápido y el OCR batch.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import pymupdf as fitz  # PyMuPDF (API moderna)
import pymupdf4llm

from ..models import (Document, DocumentMetadata, OcrStatus, SourceType,
                      count_words, make_doc_id)

log = logging.getLogger("webharvest.extract.pdf")


@dataclass
class OcrSignals:
    n_pages: int
    avg_chars_per_page: float
    image_area_ratio: float      # 0..1 promedio de área cubierta por imágenes
    pages_without_text: int
    needs_ocr: bool
    reason: str
    # Calidad de la capa de texto existente (OCR upstream malo): fracción de
    # páginas muestreadas cuyo texto es mayormente basura, y mediana del
    # ratio de palabras "normales" por página.
    bad_text_page_ratio: float = 0.0
    median_wordy_ratio: float = 1.0


_WORDY_TOKEN = re.compile(r"[A-Za-zÁÉÍÓÚÑáéíóúñüÜçÇàèìòùâêîôûäëïöü]{3,}")
# números, montos, fechas, porcentajes, códigos numéricos: contenido
# legítimo de tablas/nóminas — neutral, no cuenta como basura
_NUMERIC_TOKEN = re.compile(r"[\d.,;:%$€°#/()\[\]\-–]+")


def _page_wordy_ratio(text: str) -> Optional[float]:
    """Fracción de tokens-texto que parecen palabras reales (letras, 3+).

    Independiente del idioma dentro de alfabetos latinos; una página con
    OCR upstream corrupto ("Economi for Lati iii©r") baja de ~0.45.
    Los tokens numéricos (tablas, nóminas, montos) son neutrales: se
    excluyen del denominador para no confundir una tabla con basura.
    Devuelve None si queda muy poco texto no-numérico para juzgar.
    """
    tokens = text.split()
    non_numeric = [t for t in tokens if not _NUMERIC_TOKEN.fullmatch(t)]
    if len(non_numeric) < 30:
        return None
    wordy = sum(1 for t in non_numeric
                if _WORDY_TOKEN.fullmatch(t.strip(".,;:()[]\"'¡!¿?%")))
    return wordy / len(non_numeric)


def analyze_ocr_need(doc: fitz.Document,
                     chars_threshold: float = 120,
                     image_ratio_threshold: float = 0.85,
                     sample_pages: int = 8) -> OcrSignals:
    """Heurística de triaje: ¿este PDF necesita OCR?"""
    n = doc.page_count
    idxs = list(range(n)) if n <= sample_pages else [
        round(i * (n - 1) / (sample_pages - 1)) for i in range(sample_pages)
    ]

    total_chars, total_img_ratio, pages_no_text = 0, 0.0, 0
    wordy_ratios: list[float] = []
    for i in idxs:
        page = doc.load_page(i)
        text = page.get_text("text") or ""
        chars = len(text.strip())
        total_chars += chars
        if chars < 20:
            pages_no_text += 1
        wr = _page_wordy_ratio(text)
        if wr is not None:
            wordy_ratios.append(wr)

        page_area = abs(page.rect) or 1.0
        img_area = 0.0
        for img in page.get_image_info():
            bbox = fitz.Rect(img["bbox"])
            img_area += abs(bbox & page.rect)
        total_img_ratio += min(img_area / page_area, 1.0)

    sampled = len(idxs) or 1
    avg_chars = total_chars / sampled
    avg_img_ratio = total_img_ratio / sampled

    bad_pages = sum(1 for r in wordy_ratios if r < 0.45)
    bad_ratio = bad_pages / len(wordy_ratios) if wordy_ratios else 0.0
    median_wordy = (sorted(wordy_ratios)[len(wordy_ratios) // 2]
                    if wordy_ratios else 1.0)

    needs, reason = False, "capa de texto suficiente"
    if avg_chars < chars_threshold and avg_img_ratio > 0.5:
        needs, reason = True, (f"texto escaso ({avg_chars:.0f} chars/pág) "
                               f"con alta cobertura de imagen ({avg_img_ratio:.0%})")
    elif pages_no_text / sampled > 0.5:
        needs, reason = True, f"{pages_no_text}/{sampled} páginas muestreadas sin texto"
    elif avg_img_ratio > image_ratio_threshold and avg_chars < chars_threshold * 3:
        needs, reason = True, "documento dominado por imágenes de página completa"
    elif bad_ratio > 0.5:
        # hay capa de texto, pero es basura (OCR upstream corrupto)
        needs, reason = True, (f"capa de texto mayormente corrupta "
                               f"({bad_pages}/{len(wordy_ratios)} páginas ruidosas)")
    elif bad_ratio > 0.15:
        reason = (f"capa de texto usable pero con {bad_pages}/{len(wordy_ratios)} "
                  f"páginas ruidosas (revisar text_quality)")

    return OcrSignals(n, avg_chars, round(avg_img_ratio, 3), pages_no_text,
                      needs, reason,
                      bad_text_page_ratio=round(bad_ratio, 3),
                      median_wordy_ratio=round(median_wordy, 3))


def _pdf_native_metadata(doc: fitz.Document) -> dict:
    meta = doc.metadata or {}
    return {
        "title": (meta.get("title") or "").strip() or None,
        "author": (meta.get("author") or "").strip() or None,
        "subject": (meta.get("subject") or "").strip() or None,
        "keywords": [k.strip() for k in (meta.get("keywords") or "").replace(";", ",").split(",") if k.strip()],
        "creation_date": meta.get("creationDate") or None,
    }


def extract_pdf_document(url: str, pdf_bytes: bytes,
                         chars_threshold: float = 120,
                         image_ratio_threshold: float = 0.85) -> Document:
    """PDF -> Document. Si necesita OCR, vuelve sin markdown y marcado PENDING."""
    doc_id = make_doc_id(url)
    fdoc = fitz.open(stream=pdf_bytes, filetype="pdf")
    native = _pdf_native_metadata(fdoc)
    signals = analyze_ocr_need(fdoc, chars_threshold, image_ratio_threshold)

    metadata = DocumentMetadata(
        doc_id=doc_id,
        url=url,
        title=native["title"],
        authors=[native["author"]] if native["author"] else [],
        description=native["subject"],
        keywords=native["keywords"],
        published_date=native["creation_date"],
        source_type=SourceType.PDF.value,
        n_pages=signals.n_pages,
        ocr_signals={
            "avg_chars_per_page": round(signals.avg_chars_per_page, 1),
            "image_area_ratio": signals.image_area_ratio,
            "pages_without_text": signals.pages_without_text,
            "bad_text_page_ratio": signals.bad_text_page_ratio,
            "median_wordy_ratio": signals.median_wordy_ratio,
            "reason": signals.reason,
        },
    )

    if signals.needs_ocr:
        log.info("PDF %s requiere OCR: %s -> encolado", url, signals.reason)
        metadata.ocr_status = OcrStatus.PENDING.value
        fdoc.close()
        return Document(metadata=metadata, markdown="", raw_bytes=pdf_bytes)

    # Extracción de calidad con pymupdf4llm (markdown con estructura)
    try:
        body_md = pymupdf4llm.to_markdown(fdoc)
    except Exception as exc:
        log.warning("pymupdf4llm falló (%s); fallback a texto plano", exc)
        body_md = "\n\n".join(fdoc.load_page(i).get_text("text") for i in range(fdoc.page_count))
    fdoc.close()

    metadata.ocr_status = OcrStatus.NOT_NEEDED.value
    metadata.word_count = count_words(body_md)

    front = (
        f"---\n"
        f"doc_id: {doc_id}\n"
        f"title: {metadata.title or 'Sin título'}\n"
        f"url: {url}\n"
        f"pages: {signals.n_pages}\n"
        f"source: pdf\n"
        f"---\n\n"
    )
    return Document(metadata=metadata, markdown=front + body_md.strip() + "\n",
                    raw_bytes=pdf_bytes)
