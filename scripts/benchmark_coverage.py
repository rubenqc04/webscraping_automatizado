#!/usr/bin/env python3
"""Benchmark de cobertura de un sitio: ¿el pipeline llegó a todo?

Cruza el libro de visitas del corpus (<data_dir>/_ledger/visits.jsonl) con
un INVENTARIO independiente del sitio y reporta alcance / descarga / recall
/ precisión, más la lista de URLs faltantes con la etapa en que se perdieron
(ver webharvest/analysis/coverage.py para la definición exacta).

Fuentes de inventario (combinables; cada URL recuerda de dónde salió):
  --sitemap            sitemap.xml del sitio (lo que el dueño declara tener)
  --wget               rastreo de referencia con `wget --spider -r`: enumera
                       enlaces sin guardar nada, respeta robots.txt
  --wget-log FILE      idem, a partir de un log de wget ya existente
  --inventory FILE     una URL por línea (API de la institución, lista manual)

Sin inventario, imprime solo el embudo del propio crawl (qué vio y qué hizo).

Uso:
  python scripts/benchmark_coverage.py data_prod/x --site https://sitio.cl/ --sitemap --wget
  python scripts/benchmark_coverage.py data_prod/x --site https://sitio.cl/ --inventory urls.txt
  python scripts/benchmark_coverage.py data_prod/x --site https://sitio.cl/          # solo embudo
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webharvest.analysis.coverage import (compare, funnel, host_of,  # noqa: E402
                                          ledger_by_url, normalize,
                                          parse_wget_log)
from webharvest.discovery.crawler import discover_sitemap_urls  # noqa: E402
from webharvest.storage.ledger import read_ledger  # noqa: E402

UA = "Mozilla/5.0 (compatible; WebHarvestBenchmark/1.0; coverage reference crawl)"


def inventory_wget(site: str, depth: int, wait: float, max_urls: int,
                   log_path: Path | None) -> list[str]:
    """Rastreo de referencia. --spider no guarda archivos; wget respeta
    robots.txt por defecto (queremos medir lo que se PUEDE recolectar)."""
    log = log_path or Path(tempfile.mkstemp(suffix=".wget.log")[1])
    cmd = ["wget", "--spider", "-r", f"-l{depth}", "--no-parent", "--no-verbose",
           f"--wait={wait}", "--random-wait", f"--user-agent={UA}",
           "--reject=jpg,jpeg,png,gif,svg,webp,ico,css,js,woff,woff2,ttf,mp3,mp4,zip",
           "--reject-regex=[?&](s|q|share|replytocom|utm_\\w+)=",
           "-o", str(log), site]
    print(f"[wget] {' '.join(cmd[:-3])} ... (log: {log})", file=sys.stderr)
    subprocess.run(cmd, check=False)
    urls = parse_wget_log(log.read_text(encoding="utf-8", errors="replace"),
                          host_of(site))
    return urls[:max_urls]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir")
    ap.add_argument("--site", required=True, help="URL raíz del sitio a medir")
    ap.add_argument("--sitemap", action="store_true")
    ap.add_argument("--wget", action="store_true")
    ap.add_argument("--wget-log", type=Path,
                    help="log de un `wget --spider -r --no-verbose` ya hecho (no vuelve a rastrear)")
    ap.add_argument("--inventory", type=Path, help="archivo con una URL por línea")
    ap.add_argument("--depth", type=int, default=5, help="profundidad de wget")
    ap.add_argument("--wait", type=float, default=1.0, help="segundos entre requests de wget")
    ap.add_argument("--max-urls", type=int, default=20000)
    ap.add_argument("--utility", help="regex extra de URLs utilitarias a excluir")
    ap.add_argument("--out", type=Path, help="JSON de salida (default: <data_dir>/coverage_<host>.json)")
    ap.add_argument("--show", type=int, default=25, help="faltantes a listar en pantalla")
    a = ap.parse_args()

    data_dir = Path(a.data_dir)
    host = host_of(a.site)
    rows = read_ledger(data_dir / "_ledger" / "visits.jsonl")
    if not rows:
        sys.exit(f"No hay libro de visitas en {data_dir}/_ledger/visits.jsonl "
                 "(el corpus es anterior al ledger o el crawl no corrió)")
    index_urls = None
    idx_path = data_dir / "metadata" / "index.json"
    if idx_path.exists():
        try:
            index_urls = {e["url"] for e in json.loads(idx_path.read_text(encoding="utf-8")).values()}
        except (json.JSONDecodeError, KeyError, AttributeError):
            index_urls = None
    by_url = ledger_by_url(rows, host=host, index_urls=index_urls)
    fun = funnel(by_url)

    print(f"\n== Embudo del crawl en {host} ==")
    print(f"URLs vistas: {fun['urls_seen']}  descargadas: {fun['fetched']}  "
          f"guardadas: {fun['saved']}")
    for k, v in fun["by_decision"].items():
        print(f"  {k:<22} {v}")

    inventory: dict[str, str] = {}
    if a.sitemap:
        urls = discover_sitemap_urls(a.site, UA, limit=a.max_urls)
        urls = [u for u in urls if host_of(u) == host]
        for u in urls:
            inventory.setdefault(normalize(u), "sitemap")
        print(f"[sitemap] {len(urls)} URLs", file=sys.stderr)
    if a.wget:
        urls = inventory_wget(a.site, a.depth, a.wait, a.max_urls,
                              data_dir / f"coverage_{host}.wget.log")
        for u in urls:
            inventory.setdefault(normalize(u), "wget")
        print(f"[wget] {len(urls)} URLs con 2xx", file=sys.stderr)
    if a.wget_log:
        urls = parse_wget_log(a.wget_log.read_text(encoding="utf-8", errors="replace"), host)
        for u in urls:
            inventory.setdefault(normalize(u), "wget")
        print(f"[wget-log] {len(urls)} URLs", file=sys.stderr)
    if a.inventory:
        n = 0
        for line in a.inventory.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                inventory.setdefault(normalize(line), a.inventory.name); n += 1
        print(f"[{a.inventory.name}] {n} URLs", file=sys.stderr)

    report = {"site": a.site, "host": host, "funnel": fun}
    if inventory:
        extra = re.compile(a.utility, re.I) if a.utility else None
        cmp_ = compare(inventory, by_url, extra)
        report["coverage"] = cmp_
        print(f"\n== Cobertura vs inventario ({cmp_['inventory_total']} URLs: "
              f"{cmp_['inventory_content']} de contenido extraíble, "
              f"{cmp_['inventory_utility']} utilitarias y "
              f"{cmp_.get('inventory_no_extraible', 0)} sin extractor, excluidas) ==")
        print(f"  alcance   {cmp_['reach_pct']:>6}%   ({cmp_['seen']} vistas)")
        print(f"  descarga  {cmp_['fetch_pct']:>6}%   ({cmp_['fetched']} descargadas)")
        print(f"  recall    {cmp_['recall_pct']:>6}%   ({cmp_['saved']} guardadas)")
        print(f"  precisión {cmp_['precision_pct']:>6}%   ({cmp_['saved_total']} guardadas en total, "
              f"{cmp_['saved_outside_inventory']} fuera del inventario)")
        print("\n  faltantes por etapa:", cmp_["missing_by_stage"])
        print("  faltantes por motivo:", cmp_["missing_by_why"])
        if cmp_["missing"]:
            print(f"\n  primeras {min(a.show, len(cmp_['missing']))} faltantes:")
            for m in cmp_["missing"][:a.show]:
                print(f"   [{m['stage']:<13}] {m['url']}  <- {m['why']}")

    out = a.out or data_dir / f"coverage_{host}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReporte: {out}")


if __name__ == "__main__":
    main()
