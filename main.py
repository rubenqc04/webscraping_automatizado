#!/usr/bin/env python3
"""CLI de WebHarvest.

Uso:
    python main.py                                  # usa seeds de config.yaml
    python main.py --url https://sitio.com/nota     # una o varias URLs puntuales
    python main.py --config otra_config.yaml
    python main.py --ocr-report                     # ver la cola de OCR pendiente
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

from webharvest.pipeline import ScrapePipeline


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.WARNING)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def show_ocr_report(cfg: dict) -> None:
    queue_path = Path(cfg["storage"]["base_dir"]) / cfg["storage"]["ocr_pending_file"]
    if not queue_path.exists():
        print("No hay PDFs pendientes de OCR.")
        return
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    print(f"PDFs pendientes de OCR: {len(queue)}\n")
    for item in queue:
        print(f"- [{item['doc_id']}] {item['url']}")
        print(f"    páginas: {item.get('n_pages')} | señales: {item['signals'].get('reason')}")
        for s in item.get("suggested_strategies", []):
            print(f"    estrategia sugerida: {s}")
        print()


def load_seeds_file(path: Path) -> tuple[list[str], dict[str, dict]]:
    """Semillas desde .txt (una URL por línea) o .csv (p.ej. search_links).

    En CSV se detecta la columna cuyo valor empieza con http; el resto de
    columnas de la fila se conserva como contexto de la semilla (para
    trazabilidad: qué consulta dirigida produjo cada enlace).
    """
    import csv

    seeds: list[str] = []
    context: dict[str, dict] = {}
    text = path.read_text(encoding="utf-8-sig")

    if path.suffix.lower() == ".csv":
        rows = list(csv.reader(text.splitlines()))
        if not rows:
            return [], {}
        header = rows[0]
        url_col = next((i for i, h in enumerate(header)
                        if h.strip().lower() in ("url", "link", "enlace")), None)
        body = rows[1:] if url_col is not None else rows
        if url_col is None:      # sin header reconocible: primera celda con http
            url_col = next((i for i, cell in enumerate(rows[0])
                            if cell.strip().startswith("http")), 0)
            header = [f"col{i}" for i in range(len(rows[0]))]
        for row in body:
            if len(row) <= url_col:
                continue
            url = row[url_col].strip()
            if not url.startswith("http") or url in context:
                continue
            seeds.append(url)
            ctx = {header[i].strip(): row[i].strip()
                   for i in range(min(len(header), len(row)))
                   if i != url_col and row[i].strip()}
            if ctx:
                context[url] = ctx
    else:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.startswith("http"):
                seeds.append(line)

    # dedupe conservando orden
    return list(dict.fromkeys(seeds)), context


def main() -> int:
    parser = argparse.ArgumentParser(description="WebHarvest: scraping adaptativo y respetuoso")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--url", action="append", default=[],
                        help="URL(s) a procesar en lugar de las semillas del config")
    parser.add_argument("--url-file", type=Path, default=None,
                        help="archivo de semillas: .txt (una URL por línea, # comenta) "
                             "o .csv (detecta la columna URL; las demás columnas se "
                             "adjuntan a cada documento como extra.seed_context — "
                             "p.ej. la Consulta_Origen de search_links)")
    parser.add_argument("--ocr-report", action="store_true",
                        help="Muestra la cola de PDFs pendientes de OCR y sale")
    parser.add_argument("--report", action="store_true",
                        help="Genera corpus_report.json (agregados + lista de documentos) y sale")
    parser.add_argument("--workers", type=int, default=1,
                        help="crawl paralelo: N dominios a la vez (cada dominio "
                             "conserva su propio crawl-delay). 1 = secuencial")
    parser.add_argument("--resume", action="store_true",
                        help="omite los dominios ya completados en una corrida "
                             "previa (_progress/completed_domains.json)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)

    if args.ocr_report:
        show_ocr_report(cfg)
        return 0

    if args.report:
        from webharvest.storage.report import build_corpus_report, write_corpus_report
        out = write_corpus_report(cfg["storage"])
        rep = build_corpus_report(cfg["storage"])
        print(f"Reporte: {out}")
        print(json.dumps({k: rep[k] for k in
                          ("totals", "by_domain", "by_category",
                           "by_source_type", "by_language", "page_kinds_seen")},
                         indent=2, ensure_ascii=False))
        return 0

    seeds = list(args.url)
    seed_context: dict[str, dict] = {}
    if args.url_file:
        file_seeds, seed_context = load_seeds_file(args.url_file)
        seeds.extend(file_seeds)
    if not seeds:
        seeds = cfg.get("seeds", [])
    if not seeds:
        print("No hay semillas: define 'seeds' en config.yaml, o usa --url / --url-file",
              file=sys.stderr)
        return 1

    if args.workers > 1:
        from webharvest.parallel import ParallelHarvester
        harvester = ParallelHarvester(cfg, workers=args.workers,
                                      resume=args.resume)
        stats = harvester.run(seeds, seed_context=seed_context)
    else:
        pipeline = ScrapePipeline(cfg)
        try:
            stats = pipeline.run(seeds, seed_context=seed_context)
        finally:
            pipeline.close()

    print("\n================ RESUMEN ================")
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    print("==========================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
