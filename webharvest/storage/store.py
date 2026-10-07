"""Persistencia de documentos.

Layout en disco (relación por doc_id):

    data/
    ├── metadata/
    │   ├── index.json            <- índice maestro: doc_id -> resumen
    │   └── <doc_id>.json         <- metadata completa del documento
    ├── markdown/
    │   └── <doc_id>.md           <- texto extraído en Markdown
    ├── pdfs/
    │   └── <doc_id>.pdf          <- binario original descargado
    └── ocr_pending/
        └── ocr_queue.json        <- PDFs escaneados a la espera de
                                     que se elija estrategia de OCR
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

from ..models import Document, OcrStatus
from .ledger import VisitLedger

log = logging.getLogger("webharvest.storage")


class DocumentStore:
    def __init__(self, cfg_storage: dict):
        self.base = Path(cfg_storage["base_dir"])
        self.md_dir = self.base / cfg_storage["markdown_dir"]
        self.pdf_dir = self.base / cfg_storage["pdf_dir"]
        self.meta_dir = self.base / cfg_storage["metadata_dir"]
        self.ocr_queue_path = self.base / cfg_storage["ocr_pending_file"]
        self.index_path = self.base / cfg_storage["master_index"]
        self.analysis_dir = self.base / cfg_storage.get("analysis_dir", "analysis")
        # Libro de visitas (URLs vistas + decisión): base de la medición de
        # cobertura. Compartido entre workers como el resto del store.
        self.ledger: VisitLedger | None = (
            VisitLedger(self.base / "_ledger" / "visits.jsonl")
            if cfg_storage.get("ledger", True) else None)

        for d in (self.md_dir, self.pdf_dir, self.meta_dir,
                  self.analysis_dir, self.ocr_queue_path.parent):
            d.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._index: dict = self._load_json(self.index_path, default={})
        self._ocr_queue: list = self._load_json(self.ocr_queue_path, default=[])
        # hash del cuerpo -> doc_id, para no guardar el mismo texto dos veces
        # cuando dos URLs distintas sirven contenido idéntico (la auditoría
        # las reportaba como near_duplicate: /prensa/noticias y /prensa/
        # noticias/165 del mismo sitio, sin rel=canonical que lo declare).
        self._by_content: dict[str, str] = {}
        for doc_id, entry in self._index.items():
            h = entry.get("content_hash")
            if h:
                self._by_content.setdefault(h, doc_id)

    # ------------------------------------------------------------------
    @staticmethod
    def _load_json(path: Path, default):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("JSON corrupto en %s; se reinicia", path)
        return default

    @staticmethod
    def _dump_json(path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)   # escritura atómica

    # ------------------------------------------------------------------
    def already_saved(self, doc_id: str) -> bool:
        with self._lock:
            return doc_id in self._index

    @staticmethod
    def _content_hash(markdown: str) -> str | None:
        """Hash del cuerpo (sin front-matter ni espacios variables)."""
        body = markdown
        if body.startswith("---"):
            end = body.find("---", 3)
            if end != -1:
                body = body[end + 3:]
        norm = " ".join(body.split())
        if len(norm) < 200:          # textos muy cortos: no deduplicar
            return None
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]

    def save(self, doc: Document) -> bool:
        """Guarda el documento. Devuelve False si su texto ya existía bajo
        otro doc_id (se registra la URL como alias y no se duplica)."""
        meta = doc.metadata
        doc_id = meta.doc_id

        # Deduplicación por contenido: si este texto ya está guardado bajo otro
        # doc_id, se registra la URL como alias del original y no se duplica.
        content_hash = self._content_hash(doc.markdown) if doc.markdown else None
        if content_hash:
            with self._lock:
                twin = self._by_content.get(content_hash)
            if twin and twin != doc_id:
                self._add_alias(twin, meta.url)
                log.info("Duplicado por contenido: %s == %s (alias registrado)",
                         doc_id, twin)
                return False

        # 1) Markdown (si hay texto; los PDFs pendientes de OCR no lo tienen)
        if doc.markdown:
            md_path = self.md_dir / f"{doc_id}.md"
            md_path.write_text(doc.markdown, encoding="utf-8")
            meta.markdown_file = str(md_path.relative_to(self.base))

        # 2) Binario original (PDF, DOCX, ...)
        if doc.raw_bytes:
            ext = getattr(doc, "raw_ext", ".pdf") or ".pdf"
            pdf_path = self.pdf_dir / f"{doc_id}{ext}"
            pdf_path.write_bytes(doc.raw_bytes)
            meta.pdf_file = str(pdf_path.relative_to(self.base))

        # 3) Metadata individual
        self._dump_json(self.meta_dir / f"{doc_id}.json", meta.to_dict())

        # 4) Índice maestro + cola de OCR
        with self._lock:
            self._index[doc_id] = {
                "url": meta.url,
                "title": meta.title,
                "category": meta.category,
                "source_type": meta.source_type,
                "ocr_status": meta.ocr_status,
                "markdown_file": meta.markdown_file,
                "pdf_file": meta.pdf_file,
                "extracted_at": meta.extracted_at,
            }
            if content_hash:
                self._index[doc_id]["content_hash"] = content_hash
                self._by_content.setdefault(content_hash, doc_id)
            self._dump_json(self.index_path, self._index)

            if meta.ocr_status == OcrStatus.PENDING.value:
                self._ocr_queue.append({
                    "doc_id": doc_id,
                    "url": meta.url,
                    "pdf_file": meta.pdf_file,
                    "n_pages": meta.n_pages,
                    "signals": meta.ocr_signals,
                    "suggested_strategies": _suggest_ocr_strategies(meta),
                })
                self._dump_json(self.ocr_queue_path, self._ocr_queue)

        log.info("Guardado %s (%s) -> %s", doc_id, meta.source_type,
                 meta.title or meta.url)
        return True

    def _add_alias(self, doc_id: str, url: str) -> None:
        """Anota que `url` sirve el mismo contenido que un doc ya guardado."""
        meta_path = self.meta_dir / f"{doc_id}.json"
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        aliases = meta.setdefault("extra", {}).setdefault("duplicate_urls", [])
        if url not in aliases and url != meta.get("url"):
            aliases.append(url)
            self._dump_json(meta_path, meta)

    def save_analysis(self, doc_id: str, analysis: dict) -> None:
        """Mapa semántico de página (secciones, listado, fulltext links)."""
        self._dump_json(self.analysis_dir / f"{doc_id}.pagemap.json", analysis)

    def stats(self) -> dict:
        # bajo lock: en modo paralelo otro worker puede estar escribiendo el
        # índice, e iterar un dict mientras cambia de tamaño lanza RuntimeError
        with self._lock:
            by_type: dict[str, int] = {}
            for entry in self._index.values():
                by_type[entry["source_type"]] = by_type.get(entry["source_type"], 0) + 1
            return {
                "total_documents": len(self._index),
                "by_source_type": by_type,
                "ocr_pending": len(self._ocr_queue),
            }


def _suggest_ocr_strategies(meta) -> list[str]:
    """Sugerencias para el paso posterior de OCR, según las señales."""
    n = meta.n_pages or 0
    tips = []
    if n <= 30:
        tips.append("ocrmypdf --language spa+eng (rápido, local, mantiene el PDF)")
        tips.append("modelo de visión (VLM) si hay tablas/figuras complejas")
    else:
        tips.append("batch con tesseract/ocrmypdf en workers paralelos")
        tips.append("servicio gestionado (p. ej. Document AI / Textract) si el volumen lo justifica")
    if (meta.ocr_signals or {}).get("image_area_ratio", 0) > 0.9:
        tips.append("verificar orientación/deskew antes del OCR")
    return tips
