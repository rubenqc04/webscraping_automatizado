#!/usr/bin/env python3
"""Árbol del sitio reconstruido desde el grafo de procedencia del crawl.

Une `analysis/*-provenance.pagemap.json` (cómo se descubrió cada URL) con
el índice de documentos (qué se guardó) y responde: ¿qué sección/rama
produjo qué documentos, a qué profundidad, y dónde se cortó?

Uso:
    python scripts/site_tree.py <data_dir> [--max-depth 4] [--json salida.json]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse


def load(data_dir: Path) -> tuple[dict, dict]:
    nodes: dict[str, dict] = {}
    for f in (data_dir / "analysis").glob("*-provenance.pagemap.json"):
        nodes.update(json.loads(f.read_text(encoding="utf-8")).get("nodes", {}))
    docs: dict[str, dict] = {}
    index_file = data_dir / "metadata/index.json"
    if index_file.exists():
        for doc_id, entry in json.loads(index_file.read_text(encoding="utf-8")).items():
            meta_f = data_dir / f"metadata/{doc_id}.json"
            if meta_f.exists():
                m = json.loads(meta_f.read_text(encoding="utf-8"))
                docs[m["url"]] = {"doc_id": doc_id, "title": m.get("title"),
                                  "words": m.get("word_count"),
                                  "type": m.get("source_type")}
    return nodes, docs


def build_tree(nodes: dict, docs: dict) -> dict:
    children = defaultdict(list)
    roots = []
    for url, node in nodes.items():
        if node.get("via"):
            children[node["via"]].append(url)
        else:
            roots.append(url)

    def subtree(url: str, seen: set) -> dict:
        if url in seen:
            return {"url": url, "cycle": True}
        seen.add(url)
        node = nodes.get(url, {})
        out = {"url": url, "kind": node.get("kind"), "depth": node.get("depth")}
        if node.get("section"):
            out["section"] = node["section"]
        if url in docs:
            out["document"] = docs[url]
        kids = [subtree(c, seen) for c in children.get(url, [])]
        if kids:
            out["children"] = kids
        # agregados de la rama
        out["branch_docs"] = (1 if url in docs else 0) + sum(
            k.get("branch_docs", 0) for k in kids)
        out["branch_words"] = (docs.get(url, {}).get("words") or 0) + sum(
            k.get("branch_words", 0) for k in kids)
        return out

    seen: set = set()
    return {"roots": [subtree(r, seen) for r in roots],
            "urls_descubiertas": len(nodes),
            "documentos": len(docs)}


def print_tree(node: dict, max_depth: int, level: int = 0) -> None:
    if level > max_depth:
        return
    tag = node.get("section") or node.get("kind") or "?"
    mark = ""
    if node.get("document"):
        d = node["document"]
        mark = f"  📄 {d['words']} palabras | {(d['title'] or '')[:45]}"
    path = urlparse(node["url"]).path or "/"
    print("  " * level + f"[{tag}] {path[:70]}"
          + (f" (rama: {node['branch_docs']} docs, {node['branch_words']:,} palabras)"
             if node.get("children") else "") + mark)
    for kid in node.get("children", []):
        print_tree(kid, max_depth, level + 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    nodes, docs = load(args.data_dir)
    if not nodes:
        print("Sin grafo de procedencia (corre un crawl con la versión actual).")
        return 1
    tree = build_tree(nodes, docs)
    out = args.json or (args.data_dir / "site_tree.json")
    out.write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"URLs descubiertas: {tree['urls_descubiertas']} | documentos: {tree['documentos']}")
    for root in tree["roots"]:
        print_tree(root, args.max_depth)
    print(f"\nÁrbol completo en {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
