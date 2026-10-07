#!/usr/bin/env python3
"""Métricas operacionales de una corrida a partir de su log INFO.

Complementa a `--report` (que mira el corpus resultante) con la vista de
PROCESO: cuántas páginas se tocaron, cuántas escalaron a render, cuántas
fallaron o quedaron vacías, throughput y desglose de tipos de página.

Uso:
    python scripts/run_stats.py <run.log> [--data-dir data_prod/sitio]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

TS = re.compile(r"^(\d{2}:\d{2}:\d{2})")


def parse_log(path: Path) -> dict:
    kinds, events = Counter(), Counter()
    first_ts = last_ts = None
    sections: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TS.match(line)
        if m:
            last_ts = m.group(1)
            first_ts = first_ts or last_ts
        if "PageMap" in line:
            events["paginas_html_analizadas"] += 1
            km = re.search(r"kind=(\w+)", line)
            if km:
                kinds[km.group(1)] += 1
        elif "Escalando a render dinámico" in line:
            events["escaladas_a_playwright"] += 1
        elif "Guardado" in line:
            events["documentos_guardados"] += 1
            if "(pdf)" in line:
                events["pdfs"] += 1
            elif "(docx)" in line:
                events["docx"] += 1
        elif "Sin contenido sustantivo" in line:
            events["extracciones_vacias"] += 1
        elif "robots.txt prohíbe" in line:
            events["omitidas_por_robots"] += 1
        elif "requiere OCR" in line:
            events["encoladas_ocr"] += 1
        elif "demasiado grande" in line:
            events["rechazadas_por_tamano"] += 1
        elif "Portadilla descartada" in line:
            events["portadillas_descartadas"] += 1
        elif "Fallo de red" in line or "falló" in line.lower():
            events["fallos_red_o_render"] += 1
        elif "marcado como bloqueado" in line:
            events["dominios_bloqueados"] += 1
        elif re.search(r"Sección '([^']+)'", line):
            sections.append(re.search(r"Sección '([^']+)': (\d+)", line).groups())

    elapsed_s = None
    if first_ts and last_ts:
        f = datetime.strptime(first_ts, "%H:%M:%S")
        l = datetime.strptime(last_ts, "%H:%M:%S")
        elapsed_s = (l - f).seconds
    return {"events": dict(events), "page_kinds": dict(kinds),
            "elapsed_seconds": elapsed_s,
            "sections": [{"name": n, "docs": int(d)} for n, d in sections]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", type=Path)
    ap.add_argument("--data-dir", type=Path)
    args = ap.parse_args()

    stats = parse_log(args.log)
    ev, el = stats["events"], stats["elapsed_seconds"]
    pages = ev.get("paginas_html_analizadas", 0)
    if el:
        stats["throughput_paginas_por_min"] = round(pages / (el / 60), 1) if el else None

    if args.data_dir and (args.data_dir / "metadata/index.json").exists():
        idx = json.loads((args.data_dir / "metadata/index.json").read_text())
        words = 0
        for doc_id in idx:
            meta_f = args.data_dir / f"metadata/{doc_id}.json"
            if meta_f.exists():
                words += json.loads(meta_f.read_text()).get("word_count") or 0
        du = sum(f.stat().st_size for f in args.data_dir.rglob("*") if f.is_file())
        stats["corpus"] = {"documentos": len(idx), "palabras": words,
                           "disco_mb": round(du / 1_048_576, 1)}

    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
