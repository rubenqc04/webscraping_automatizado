#!/usr/bin/env python3
"""Sonda de paginación: ¿detecta el método Y avanza de verdad?

Detectar un enlace de "página siguiente" no sirve si al seguirlo no
aparecen ítems nuevos: sitios que ignoran el parámetro, que devuelven
siempre la primera página, o que paginan por JavaScript sin cambiar la URL.
Esta sonda recorre la paginación de un listado real y verifica, página por
página, que aporte ítems que no se hayan visto antes.

Salida por cada URL: método detectado, cuántas páginas avanzó, ítems nuevos
en cada una, y por qué se detuvo.

Uso:
    python scripts/probe_pagination.py URL [URL ...] [--pages 4]
    python scripts/probe_pagination.py --file urls.txt --pages 5 --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from webharvest.analysis.page_map import (build_page_map,  # noqa: E402
                                          synthesize_next_page_urls)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def method_of(url: str, pmap) -> str:
    """Nombre legible del mecanismo que se está usando."""
    if not pmap.pagination_urls:
        return "js-only" if pmap.pagination_js_only else "ninguno"
    nxt = pmap.pagination_urls[0]
    q = urlparse(nxt).query
    m = re.search(r"(?:^|&)([A-Za-z_]+)=\d+", q)
    if m:
        return f"?{m.group(1)}=N"
    if re.search(r"/(page|pagina|p[áa]g|pag)/\d+/?$", urlparse(nxt).path, re.I):
        return "/page/N/"
    if q:
        return "query opaca"
    return "número en la ruta"


def probe(client: httpx.Client, url: str, max_pages: int, delay: float) -> dict:
    """Recorre la paginación como lo hace el pipeline: encola TODAS las
    candidatas y las visita, en vez de seguir solo la primera.

    Importa porque en Drupal `?page=0` ES la primera página: seguir el
    primer enlace del paginador devuelve lo mismo que ya se tenía. Y una
    candidata sintetizada que da 404 no significa que no haya paginación,
    solo que esa convención no era la del sitio: hay que probar la otra.
    """
    out = {"url": url, "pages": [], "method": None, "stopped": None,
           "errors": 0}
    seen_items: set[str] = set()
    visited: set[str] = set()
    queue: list[tuple[str, bool]] = [(url, False)]      # (url, sintetizada)
    barren = 0                                          # páginas seguidas sin aportar

    while queue and len(out["pages"]) < max_pages:
        current, synth = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        try:
            r = client.get(current)
        except Exception as exc:
            out["errors"] += 1
            out.setdefault("last_error", f"{type(exc).__name__}")
            time.sleep(delay)
            continue
        if r.status_code >= 400:
            out["errors"] += 1
            out.setdefault("last_error", f"HTTP {r.status_code}")
            time.sleep(delay)
            continue

        pmap = build_page_map(str(r.url), r.text, {})
        items = {it.url for it in pmap.listing_items}
        new = items - seen_items
        seen_items |= items
        out["pages"].append({
            "n": len(out["pages"]) + 1, "url": current, "kind": pmap.page_kind,
            "items": len(items), "new_items": len(new), "synthesized": synth,
        })
        if out["method"] is None:
            out["method"] = method_of(current, pmap)

        if out["pages"] and len(out["pages"]) > 1:
            barren = barren + 1 if not new else 0
            if barren >= 3:
                out["stopped"] = "3 páginas seguidas sin ítems nuevos"
                break

        # encolar lo que esta página declare, y si no declara nada, sintetizar
        declared = [u for u in pmap.pagination_urls if u not in visited]
        for u in declared:
            if (u, False) not in queue:
                queue.append((u, False))
        if not declared and new:
            for u in synthesize_next_page_urls(current):
                if u not in visited and (u, True) not in queue:
                    queue.append((u, True))
        time.sleep(delay)

    if out["stopped"] is None:
        out["stopped"] = (f"tope de {max_pages} páginas de la sonda"
                          if len(out["pages"]) >= max_pages
                          else "cola de candidatas agotada")
    out["total_unique_items"] = len(seen_items)
    out["pages_advanced"] = len(out["pages"])
    out["pages_with_new"] = sum(1 for p in out["pages"][1:] if p["new_items"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--file", type=Path, help="archivo con una URL por línea")
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()

    urls = list(a.urls)
    if a.file:
        urls += [l.strip() for l in a.file.read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
    if not urls:
        ap.error("da al menos una URL, o --file")

    results = []
    with httpx.Client(headers={"User-Agent": UA}, timeout=30,
                      follow_redirects=True) as c:
        for u in urls:
            res = probe(c, u, a.pages, a.delay)
            results.append(res)
            host = urlparse(u).netloc.replace("www.", "")[:30]
            seq = " ".join(f"{p['new_items']}{'*' if p['synthesized'] else ''}"
                           for p in res["pages"][:8])
            err = f" {res['errors']}err" if res["errors"] else ""
            print(f"{host:30} {str(res['method']):16} "
                  f"{res['pages_advanced']:2d}p{err:6} nuevos: {seq:26} "
                  f"total {res['total_unique_items']:4d}  [{res['stopped'][:34]}]")
            time.sleep(a.delay)

    ok = [r for r in results if r["pages_with_new"] > 0]
    print(f"\n{len(ok)}/{len(results)} avanzaron de verdad "
          f"(2+ páginas con ítems nuevos).  * = página sintetizada")
    if a.json:
        a.json.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"Detalle: {a.json}")


if __name__ == "__main__":
    main()
