#!/usr/bin/env python3
"""Contraste: scraping cognitivo vs API oficial (Europe PMC).

Para la misma consulta, compara lo que obtuvimos crawleando la web UI
(data_europepmc/) contra lo que entrega la REST API oficial
(https://europepmc.org/RestfulWebService) — cobertura, campos y costo
en requests. Sirve para decidir cuándo NO scrapear: si el sitio ofrece
API, esa es la vía correcta en producción.

Uso:
    python scripts/compare_api_vs_scraping.py [--query "SRC:*"] [--n 25]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def api_results(query: str, n: int) -> list[dict]:
    resp = httpx.get(API, params={
        "query": query, "format": "json", "pageSize": n,
        "resultType": "core", "sort": "P_PDATE_D desc",
    }, timeout=30)
    resp.raise_for_status()
    out = []
    for r in resp.json().get("resultList", {}).get("result", []):
        out.append({
            "id": r.get("id"),
            "source": r.get("source"),
            "title": r.get("title"),
            "has_abstract": bool(r.get("abstractText")),
            "is_open_access": r.get("isOpenAccess") == "Y",
            "has_fulltext_xml": "MED" != r.get("source") or r.get("inEPMC") == "Y",
            "authors": r.get("authorString"),
            "journal": (r.get("journalInfo") or {}).get("journal", {}).get("title"),
            "doi": r.get("doi"),
        })
    return out


def scraped_docs(data_dir: Path) -> list[dict]:
    docs = []
    index = json.loads((data_dir / "metadata/index.json").read_text())
    for doc_id, entry in index.items():
        m = json.loads((data_dir / f"metadata/{doc_id}.json").read_text())
        match = re.search(r"/article/([A-Z]+)/(\w+)", m.get("url") or "")
        docs.append({
            "doc_id": doc_id,
            "source": match.group(1) if match else None,
            "ext_id": match.group(2) if match else None,
            "title": m.get("title"),
            "source_type": m.get("source_type"),
            "word_count": m.get("word_count"),
        })
    return docs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="SRC:*")
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--data-dir", default="data_europepmc")
    args = ap.parse_args()

    api = api_results(args.query, args.n)
    scraped = scraped_docs(Path(args.data_dir))
    scraped_ids = {d["ext_id"] for d in scraped if d["ext_id"]}
    overlap = [r for r in api if r["id"] in scraped_ids]

    report = {
        "query": args.query,
        "api": {
            "requests_needed": 1,
            "results": len(api),
            "with_abstract": sum(1 for r in api if r["has_abstract"]),
            "open_access": sum(1 for r in api if r["is_open_access"]),
            "fields": ["title", "abstract", "authors", "journal", "doi",
                       "isOpenAccess", "fullTextUrlList", "citedByCount", "..."],
            "fulltext": "XML estructurado por /fullTextXML para artículos en PMC",
        },
        "scraping": {
            "documents": len(scraped),
            "by_type": {},
            "requests_aprox": "2 por página (httpx + render) + 1 por PDF",
        },
        "overlap_first_page": {
            "api_ids_tambien_scrapeados": len(overlap),
            "de": len(api),
        },
        "conclusion": (
            "Para Europe PMC la API oficial entrega en 1 request lo que el "
            "crawl obtiene en ~60, con metadata más rica y fulltext XML "
            "estructurado. El scraper cognitivo queda para sitios SIN API; "
            "cuando exista API documentada, usarla es lo correcto (y lo que "
            "el sitio prefiere)."
        ),
    }
    for d in scraped:
        t = d["source_type"]
        report["scraping"]["by_type"][t] = report["scraping"]["by_type"].get(t, 0) + 1

    out = Path(args.data_dir) / "api_vs_scraping.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nGuardado en {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
