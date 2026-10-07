#!/usr/bin/env python3
"""Evaluación comparativa de motores OCR sobre el corpus real en cola.

Clases de complejidad (PDFs reales recolectados por WebHarvest):
    A_impresion_limpia   PDF digital moderno: su capa de texto nativa es el
                         GROUND TRUTH; se rasteriza y cada motor debe
                         reconstruirla -> precisión objetiva (token-F1 y
                         similitud de caracteres).
    B_escaneo_antiguo    escaneo puro 364 págs, sin capa de texto (años 60-80)
    C_escaneo_denso      escaneo puro 112 págs, 87% imagen
    D_capa_corrupta      PDF con ToUnicode roto: el texto nativo es basura,
                         el OCR debe venir del render de la página

Motores:
    rapidocr    PP-OCR v4 vía ONNX (CPU, liviano, sin torch)
    easyocr     CRAFT + CRNN (torch CPU)
    doctr       DBNet + CRNN (torch CPU)
    paddle_vl   PaddleOCR-VL-1.6-0.9B servido en localhost:8118 (vLLM, GPU
                compartida ya desplegada en esta máquina)

Métricas:
    - segundos/página (en este hardware; CPU para los 3 primeros)
    - chars y tokens producidos
    - wordy_ratio: fracción de tokens no numéricos que parecen palabras
      (misma métrica de calidad del pipeline)
    - clase A: token_f1 y char_similarity contra el ground truth
    - clases B/C/D: token_f1 de a pares (consenso entre motores)

Salida: output/results.json + output/samples/<clase>_<motor>.txt
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).parent
OUT = ROOT / "output"
SAMPLES = OUT / "samples"
DATA = ROOT.parent

DPI = 220
PAGES_PER_DOC = 2          # equilibrio costo/señal en CPU

DOCS = {
    "A_impresion_limpia": (DATA / "data_battery/cepal/pdfs/db16754f021792c8.pdf", [10, 30]),
    "B_escaneo_antiguo": (DATA / "data_prod/cepal/pdfs/bf9e61c9574bd00a.pdf", [30, 180]),
    "C_escaneo_denso": (DATA / "data_prod/cepal/pdfs/1014b22db18cfb93.pdf", [20, 60]),
    "D_capa_corrupta": (DATA / "data_prod/cepal/pdfs/50fc31f0c712cf63.pdf", [40, 90]),
}

_WORDY = re.compile(r"[A-Za-zÁÉÍÓÚÑáéíóúñüÜ]{3,}")
_NUM = re.compile(r"[\d.,;:%$€°#/()\[\]\-–]+")


def wordy_ratio(text: str) -> float:
    toks = [t for t in text.split() if not _NUM.fullmatch(t)]
    if len(toks) < 10:
        return 0.0
    return sum(1 for t in toks if _WORDY.fullmatch(t.strip(".,;:()[]\"'¡!¿?%"))) / len(toks)


def norm_tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def token_f1(a: str, b: str) -> float:
    ta, tb = Counter(norm_tokens(a)), Counter(norm_tokens(b))
    inter = sum((ta & tb).values())
    if not inter:
        return 0.0
    p, r = inter / sum(tb.values()), inter / sum(ta.values())
    return 2 * p * r / (p + r)


def char_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, " ".join(a.split())[:5000],
                           " ".join(b.split())[:5000]).ratio()


# ----------------------------------------------------------------------
def render_pages() -> dict:
    """clase -> [(page_no, png_bytes, ground_truth|None)]"""
    corpus = {}
    for cls, (path, pages) in DOCS.items():
        doc = fitz.open(path)
        items = []
        for p in pages:
            p = min(p, doc.page_count - 1)
            page = doc.load_page(p)
            png = page.get_pixmap(dpi=DPI).tobytes("png")
            truth = page.get_text("text") if cls.startswith("A_") else None
            items.append((p, png, truth))
        doc.close()
        corpus[cls] = items
    return corpus


# ----------------------------------------------------------------------
# Motores (cada uno: init una vez, luego png_bytes -> texto)
# ----------------------------------------------------------------------
def make_engines() -> dict:
    engines = {}

    try:
        from rapidocr_onnxruntime import RapidOCR
        r = RapidOCR()

        def rapid(png: bytes) -> str:
            res, _ = r(png)
            return "\n".join(x[1] for x in res) if res else ""
        engines["rapidocr"] = rapid
    except Exception as e:
        print("rapidocr no disponible:", e)

    try:
        import easyocr
        import numpy as np
        from PIL import Image
        reader = easyocr.Reader(["es", "en"], gpu=False, verbose=False)

        def easy(png: bytes) -> str:
            img = np.array(Image.open(io.BytesIO(png)))
            return "\n".join(reader.readtext(img, detail=0, paragraph=True))
        engines["easyocr"] = easy
    except Exception as e:
        print("easyocr no disponible:", e)

    try:
        from doctr.io import DocumentFile
        from doctr.models import ocr_predictor
        model = ocr_predictor(pretrained=True)

        def dtr(png: bytes) -> str:
            doc = DocumentFile.from_images([png])
            out = model(doc)
            return out.render()
        engines["doctr"] = dtr
    except Exception as e:
        print("doctr no disponible:", e)

    try:
        import httpx
        client = httpx.Client(timeout=180)
        assert client.get("http://localhost:8118/v1/models").status_code == 200

        def paddle_vl(png: bytes) -> str:
            b64 = base64.b64encode(png).decode()
            resp = client.post("http://localhost:8118/v1/chat/completions", json={
                "model": "PaddleOCR-VL-1.6-0.9B",
                "messages": [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": "OCR:"},
                ]}],
                "temperature": 0.0,
                "max_tokens": 4096,
            })
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        engines["paddle_vl"] = paddle_vl
    except Exception as e:
        print("paddle_vl no disponible:", e)

    return engines


# ----------------------------------------------------------------------
def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    corpus = render_pages()
    engines = make_engines()
    print("Motores activos:", ", ".join(engines) or "NINGUNO")

    results = {cls: {} for cls in corpus}
    texts: dict[tuple, str] = {}

    for cls, items in corpus.items():
        for name, fn in engines.items():
            per_page, seconds = [], 0.0
            joined = []
            for page_no, png, truth in items:
                t0 = time.perf_counter()
                try:
                    text = fn(png)
                except Exception as e:
                    text = ""
                    print(f"  {cls}/{name} p{page_no}: ERROR {e}")
                dt = time.perf_counter() - t0
                seconds += dt
                joined.append(text)
                row = {"page": page_no, "seconds": round(dt, 1),
                       "chars": len(text), "tokens": len(text.split()),
                       "wordy_ratio": round(wordy_ratio(text), 3)}
                if truth:
                    row["token_f1_vs_truth"] = round(token_f1(truth, text), 3)
                    row["char_similarity"] = round(char_similarity(truth, text), 3)
                per_page.append(row)
            full = "\n\n--- page break ---\n\n".join(joined)
            texts[(cls, name)] = full
            (SAMPLES / f"{cls}_{name}.txt").write_text(full, encoding="utf-8")
            agg = {"seconds_per_page": round(seconds / len(items), 1),
                   "pages": per_page}
            if cls.startswith("A_"):
                agg["mean_token_f1"] = round(
                    sum(p["token_f1_vs_truth"] for p in per_page) / len(per_page), 3)
                agg["mean_char_similarity"] = round(
                    sum(p["char_similarity"] for p in per_page) / len(per_page), 3)
            agg["mean_wordy_ratio"] = round(
                sum(p["wordy_ratio"] for p in per_page) / len(per_page), 3)
            results[cls][name] = agg
            print(f"{cls} / {name}: {agg['seconds_per_page']}s/pág, "
                  f"wordy={agg['mean_wordy_ratio']}"
                  + (f", F1={agg.get('mean_token_f1')}" if cls.startswith("A_") else ""))

        # consenso entre motores (clases sin ground truth)
        if not cls.startswith("A_"):
            pair = {}
            names = [n for n in engines if (cls, n) in texts]
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    pair[f"{a}~{b}"] = round(token_f1(texts[(cls, a)], texts[(cls, b)]), 3)
            results[cls]["_consenso_token_f1"] = pair

    (OUT / "results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResultados en {OUT / 'results.json'} y muestras en {SAMPLES}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
