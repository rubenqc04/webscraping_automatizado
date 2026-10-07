#!/usr/bin/env python3
"""Excel ordenado de fuentes autorizadas -> CSVs de semillas para la corrida v3.

Lee la hoja `Links` de "Links_por_institucion_ORDENADO.xlsx" (columnas N°,
Institución, Link, Permiso, Comentarios) y escribe en configs/seeds/v3/:

  portadas.csv              URLs de portada (ruta vacía, "/", home o index)
  profundas.csv             URLs curadas dentro de un sitio (listados, publicaciones)
  restringidas_revisar.csv  enlaces cuyo propio comentario en la planilla dice que
                            los términos del sitio prohíben la reproducción. NO se
                            recorren por defecto: el permiso dice "implícito" y el
                            comentario lo contradice; decide legal.

Cada CSV conserva Institución, Permiso, Comentarios y N° como contexto de la
semilla, así cada documento recolectado lleva en su metadata la autorización
de la que proviene.

Uso:
    python scripts/parse_links_ordenado.py <excel> [--out configs/seeds/v3]
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from urllib.parse import urlparse

import openpyxl

RESTRICCION = re.compile(r"prohíben|prohiben|ninguna porción|todos los derechos "
                         r"reservados|no se puede reproducir", re.I)
HOME = re.compile(r"/(home|index)(\.\w+)?/?$", re.I)
COLS = ["Institucion", "URL", "Permiso", "Comentarios", "N"]


def norm(u: str) -> str:
    u = str(u).strip()
    return u if re.match(r"https?://", u, re.I) else "https://" + u


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("excel", type=Path)
    ap.add_argument("--out", type=Path, default=Path("configs/seeds/v3"))
    a = ap.parse_args()
    ws = openpyxl.load_workbook(a.excel, read_only=True, data_only=True)["Links"]
    rows = [tuple(r) + (None,) * (5 - len(r)) for r in ws.iter_rows(values_only=True)]
    # filas de datos: N° numérico y con enlace. Las filas de título/encabezado y
    # los comentarios sueltos al final (sin N° ni enlace) quedan fuera.
    links = [r for r in rows if isinstance(r[0], (int, float)) and r[2]]
    out = {"portadas": [], "profundas": [], "restringidas_revisar": []}
    seen = set()
    for n, inst, url, perm, com in links:
        u = norm(url)
        if u.rstrip("/") in seen:
            continue
        seen.add(u.rstrip("/"))
        row = {"N": int(n), "Institucion": str(inst).strip(), "URL": u,
               "Permiso": str(perm or "").strip(), "Comentarios": str(com or "").strip()}
        if com and RESTRICCION.search(str(com)):
            out["restringidas_revisar"].append(row)
            continue
        p = urlparse(u).path
        out["portadas" if p in ("", "/") or HOME.search(p) else "profundas"].append(row)
    a.out.mkdir(parents=True, exist_ok=True)
    for name, data in out.items():
        with open(a.out / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            w.writerows(data)
        doms = len({urlparse(x["URL"]).netloc for x in data})
        print(f"{name:22} {len(data):4d} URLs  {doms:4d} dominios")


if __name__ == "__main__":
    main()
