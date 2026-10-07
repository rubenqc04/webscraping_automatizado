"""Reporte consolidado del corpus para revisión humana.

Une el índice maestro, la metadata individual y los mapas de página en
un solo JSON (`corpus_report.json`) con agregados por dominio, categoría,
tipo de fuente e idioma, más la lista de documentos con sus rutas — para
responder rápido "¿qué se recolectó, de dónde, y dónde está cada texto?".
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def build_corpus_report(cfg_storage: dict) -> dict:
    base = Path(cfg_storage["base_dir"])
    meta_dir = base / cfg_storage["metadata_dir"]
    analysis_dir = base / cfg_storage.get("analysis_dir", "analysis")
    index_path = base / cfg_storage["master_index"]

    index = {}
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))

    docs = []
    by_domain, by_category, by_source, by_lang = Counter(), Counter(), Counter(), Counter()
    total_words = 0
    for doc_id in index:
        meta_file = meta_dir / f"{doc_id}.json"
        if not meta_file.exists():
            continue
        m = json.loads(meta_file.read_text(encoding="utf-8"))
        by_domain[m.get("domain") or "?"] += 1
        by_category[m.get("category") or "?"] += 1
        by_source[m.get("source_type") or "?"] += 1
        by_lang[(m.get("language") or "?").split("-")[0]] += 1
        total_words += m.get("word_count") or 0
        docs.append({
            "doc_id": doc_id,
            "title": m.get("title"),
            "url": m.get("url"),
            "domain": m.get("domain"),
            "category": m.get("category"),
            "source_type": m.get("source_type"),
            "language": m.get("language"),
            "published_date": m.get("published_date"),
            "word_count": m.get("word_count"),
            "ocr_status": m.get("ocr_status"),
            "files": {
                "markdown": m.get("markdown_file"),
                "binary": m.get("pdf_file"),
                "metadata": f"{cfg_storage['metadata_dir']}/{doc_id}.json",
            },
            "page_kind": (m.get("extra") or {}).get("page_kind"),
        })

    page_kinds = Counter()
    listing_pages = []
    if analysis_dir.exists():
        for f in analysis_dir.glob("*.pagemap.json"):
            pm = json.loads(f.read_text(encoding="utf-8"))
            page_kinds[pm.get("page_kind", "?")] += 1
            if pm.get("page_kind") == "listing":
                listing_pages.append({
                    "url": pm["url"],
                    "n_items": len(pm.get("listing_items", [])),
                    "pagination_js_only": pm.get("pagination_js_only"),
                })

    docs.sort(key=lambda d: -(d["word_count"] or 0))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_dir": str(base),
        "totals": {
            "documents": len(docs),
            "words": total_words,
            "pages_analyzed": sum(page_kinds.values()),
        },
        "by_domain": dict(by_domain.most_common()),
        "by_category": dict(by_category.most_common()),
        "by_source_type": dict(by_source.most_common()),
        "by_language": dict(by_lang.most_common()),
        "page_kinds_seen": dict(page_kinds.most_common()),
        "listings": listing_pages,
        "documents": docs,
    }


def write_corpus_report(cfg_storage: dict) -> Path:
    report = build_corpus_report(cfg_storage)
    out = Path(cfg_storage["base_dir"]) / "corpus_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
