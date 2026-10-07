#!/usr/bin/env python3
"""Procesador diferido de la cola de OCR.

El crawl principal NUNCA hace OCR: solo detecta PDFs escaneados, los
descarga y los deja registrados en data/ocr_pending/ocr_queue.json con
sus señales. Este script se ejecuta después, cuando ya elegiste una
estrategia según volumen, idioma y presupuesto:

    python process_ocr_queue.py --strategy ocrmypdf --lang spa+eng
    python process_ocr_queue.py --strategy tesseract --lang spa
    python process_ocr_queue.py --dry-run          # solo lista la cola

Estrategias incluidas:
  - ocrmypdf   : añade capa de texto al PDF y luego extrae con
                 pymupdf4llm (mejor calidad de layout). Requiere
                 `pip install ocrmypdf` y tesseract instalado.
  - tesseract  : rasteriza páginas con PyMuPDF y pasa pytesseract
                 (más simple; texto plano por página).

Punto de extensión: implementa `VlmStrategy` u otra clase con el mismo
método `run(pdf_path) -> str` para usar un modelo de visión o un
servicio gestionado.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("ocr")


# ----------------------------------------------------------------------
def strategy_ocrmypdf(pdf_path: Path, lang: str) -> str:
    """OCR con ocrmypdf (mantiene el PDF, añade capa de texto) + pymupdf4llm."""
    import subprocess
    import tempfile

    import pymupdf4llm

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        out = Path(tmp.name)
    subprocess.run(
        ["ocrmypdf", "--language", lang, "--skip-text", "--deskew",
         str(pdf_path), str(out)],
        check=True,
    )
    md = pymupdf4llm.to_markdown(str(out))
    out.unlink(missing_ok=True)
    return md


def strategy_tesseract(pdf_path: Path, lang: str) -> str:
    """Rasterizar con PyMuPDF y OCR página a página con pytesseract."""
    import io

    import pymupdf
    import pytesseract
    from PIL import Image

    doc = pymupdf.open(pdf_path)
    parts = []
    for i in range(doc.page_count):
        pix = doc.load_page(i).get_pixmap(dpi=300)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        parts.append(f"## Página {i + 1}\n\n" + pytesseract.image_to_string(img, lang=lang))
    doc.close()
    return "\n\n".join(parts)


def strategy_rapidocr(pdf_path: Path, lang: str) -> str:
    """PP-OCR vía ONNX (CPU, sin torch ni binarios del sistema).

    `lang` se ignora: los modelos PP-OCR son multilingües (latín incluido).
    Rápido y suficiente para impresión limpia; en escaneos degradados
    prefiere `paddle_vl`.
    """
    import pymupdf
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    doc = pymupdf.open(pdf_path)
    parts = []
    for i in range(doc.page_count):
        png = doc.load_page(i).get_pixmap(dpi=220).tobytes("png")
        res, _ = ocr(png)
        text = "\n".join(x[1] for x in res) if res else ""
        parts.append(f"## Página {i + 1}\n\n" + text)
    doc.close()
    return "\n\n".join(parts)


def strategy_paddle_vl(pdf_path: Path, lang: str,
                       endpoint: str = "http://localhost:8118") -> str:
    """PaddleOCR-VL servido por vLLM (OpenAI-compatible, GPU compartida).

    Modelo de visión especializado en documentos: la mejor opción para
    escaneos degradados, tablas y layouts complejos. Requiere el servidor
    `paddleocr genai_server` corriendo (en esta máquina: puerto 8118).
    """
    import base64

    import httpx
    import pymupdf

    client = httpx.Client(timeout=300)
    model = client.get(f"{endpoint}/v1/models").json()["data"][0]["id"]
    doc = pymupdf.open(pdf_path)
    parts = []
    for i in range(doc.page_count):
        png = doc.load_page(i).get_pixmap(dpi=220).tobytes("png")
        b64 = base64.b64encode(png).decode()
        resp = client.post(f"{endpoint}/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": "OCR:"},
            ]}],
            "temperature": 0.0, "max_tokens": 4096,
        })
        resp.raise_for_status()
        parts.append(f"## Página {i + 1}\n\n"
                     + resp.json()["choices"][0]["message"]["content"])
    doc.close()
    return "\n\n".join(parts)


STRATEGIES = {"ocrmypdf": strategy_ocrmypdf, "tesseract": strategy_tesseract,
              "rapidocr": strategy_rapidocr, "paddle_vl": strategy_paddle_vl}


def pick_strategy(signals: dict) -> str:
    """Enruta cada PDF al mejor motor según su complejidad (eval 2026-08-29,
    ocr-eval/output/results.json, corpus real CEPAL + control con ground truth):

      - paddle_vl  F1=0.986 en impresión limpia y la mejor lectura de escaneos
                   degradados (tildes y espaciado correctos), 3-5 s/pág vía
                   servidor GPU compartido -> default para todo lo degradado.
      - rapidocr   F1=0.87, pierde espacios/tildes en escaneos antiguos, pero
                   es local, liviano y sin GPU -> suficiente para impresión
                   moderna limpia.
      (easyocr empata calidad con paddle_vl pero 3-4x más lento en CPU:
       es el fallback autónomo si el servidor no está disponible.)
    """
    degraded = (signals.get("image_area_ratio", 0) > 0.5
                or signals.get("bad_text_page_ratio", 0) > 0.5
                or signals.get("avg_chars_per_page", 999) < 50)
    return "paddle_vl" if degraded else "rapidocr"


# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--strategy", choices=list(STRATEGIES) + ["auto"],
                        default="auto",
                        help="'auto' enruta cada PDF según sus señales de complejidad")
    parser.add_argument("--lang", default="spa+eng")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    base = Path(cfg["storage"]["base_dir"])
    queue_path = base / cfg["storage"]["ocr_pending_file"]
    md_dir = base / cfg["storage"]["markdown_dir"]
    meta_dir = base / cfg["storage"]["metadata_dir"]

    if not queue_path.exists():
        print("Cola vacía: no hay PDFs pendientes de OCR.")
        return 0
    queue = json.loads(queue_path.read_text(encoding="utf-8"))

    if args.dry_run:
        for item in queue:
            print(f"- {item['doc_id']} | {item.get('n_pages')} págs | {item['url']}")
            print(f"    señales: {item['signals'].get('reason')}")
        return 0

    remaining = []
    for item in queue:
        pdf_path = base / item["pdf_file"]
        chosen = (pick_strategy(item.get("signals") or {})
                  if args.strategy == "auto" else args.strategy)
        run = STRATEGIES[chosen]
        try:
            log.info("OCR (%s) sobre %s ...", chosen, pdf_path.name)
            md_body = run(pdf_path, args.lang)

            md_path = md_dir / f"{item['doc_id']}.md"
            front = (f"---\ndoc_id: {item['doc_id']}\nurl: {item['url']}\n"
                     f"source: pdf_ocr\nocr_strategy: {chosen}\n---\n\n")
            md_path.write_text(front + md_body.strip() + "\n", encoding="utf-8")

            # Actualizar metadata individual e índice
            meta_path = meta_dir / f"{item['doc_id']}.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.update(ocr_status="done",
                        markdown_file=str(md_path.relative_to(base)),
                        word_count=len(md_body.split()))
            meta["ocr_signals"]["strategy_used"] = chosen
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

            index_path = base / cfg["storage"]["master_index"]
            index = json.loads(index_path.read_text(encoding="utf-8"))
            if item["doc_id"] in index:
                index[item["doc_id"]].update(ocr_status="done",
                                             markdown_file=meta["markdown_file"])
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            log.info("OK -> %s", md_path)
        except Exception as exc:
            log.error("Falló %s: %s (se mantiene en cola)", item["doc_id"], exc)
            remaining.append(item)

    queue_path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"\nProcesados: {len(queue) - len(remaining)} | En cola: {len(remaining)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
