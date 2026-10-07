#!/usr/bin/env python3
"""Empaqueta un corpus recolectado como dataset consumible (JSONL).

El corpus vive como muchos archivos (`markdown/`, `metadata/`, `pdfs/`),
que es cómodo para auditar pero no para consumir. Esto produce un dataset
en JSONL — un documento por línea, texto + procedencia + derechos — que
es el formato habitual para pipelines de entrenamiento y para compartir.

Particiona por estado de derechos usando el dossier de `audit_rights.py`,
de modo que la decisión del equipo se materialice en archivos separados
en vez de quedar en un informe que hay que recordar:

    dataset/
    ├── permisivo.jsonl        sector público, Creative Commons
    ├── revisar.jsonl          sin declaración / copyright sin términos
    ├── reservado_ia.jsonl     el sitio reserva uso para IA/TDM  <-- apartado
    ├── derechos_reservados.jsonl
    ├── MANIFEST.json          conteos, palabras y política aplicada
    └── README.md              qué es cada partición y cómo se decidió

Por defecto `reservado_ia` y `derechos_reservados` se escriben aparte pero
NO se mezclan con lo demás: quedan disponibles para que el equipo decida,
sin que nadie los use por accidente. Con `--only permisivo` se exporta
solo la partición segura.

Uso:
    python scripts/export_dataset.py <data_dir> [--out DIR] [--only PART ...]
                                     [--min-words N]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# clasificación de audit_rights.py -> partición del dataset
PARTITION_OF = {
    "cc-license": "permisivo",
    "sector-publico": "permisivo",
    "sin-declaracion-visible": "revisar",
    "copyright-declarado-sin-terminos-visibles": "revisar",
    "todos-los-derechos-reservados": "derechos_reservados",
    "tdm-o-ai-reservado": "reservado_ia",
}
DEFAULT_PARTITION = "revisar"

PARTITION_DOC = {
    "permisivo": ("Sector público y licencias Creative Commons. La vía más "
                  "clara para uso en corpus; revisar de todos modos las "
                  "variantes NC/ND de CC, que restringen uso comercial y "
                  "derivadas."),
    "revisar": ("Sin declaración visible de términos, o solo una línea de "
                "copyright sin página de condiciones. Zona gris: requiere "
                "decisión de política del equipo."),
    "reservado_ia": ("El sitio expresa reserva frente a minería de datos o "
                     "entrenamiento de IA (Content-Signal ai-train=no, "
                     "tdmrep.json, meta noai, o bloqueo de bots de IA en "
                     "robots.txt). APARTADO: no usar sin decisión explícita, "
                     "incluso donde exista autorización institucional — el "
                     "conflicto entre ambas señales debe resolverse con quien "
                     "otorgó la autorización."),
    "derechos_reservados": ("Términos que declaran todos los derechos "
                           "reservados o prohíben reproducción sin permiso. "
                           "APARTADO: requiere permiso expreso."),
}


def partition_for(classification: str) -> str:
    for key, part in PARTITION_OF.items():
        if classification.startswith(key):
            return part
    return DEFAULT_PARTITION


def load_rights(data_dir: Path) -> dict[str, str]:
    """host -> partición, desde rights_report.json si existe."""
    path = data_dir / "rights_report.json"
    if not path.exists():
        return {}
    report = json.loads(path.read_text(encoding="utf-8"))
    return {d["domain"].lower(): partition_for(d["clasificacion_preliminar"])
            for d in report.get("dominios", [])}


def body_of(md: str) -> str:
    if md.startswith("---"):
        end = md.find("---", 3)
        if end != -1:
            return md[end + 3:].strip()
    return md.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--only", nargs="*", default=None,
                    help="exportar solo estas particiones (p.ej. --only permisivo)")
    ap.add_argument("--min-words", type=int, default=50,
                    help="descarta documentos con menos palabras (default 50)")
    args = ap.parse_args()

    base = args.data_dir
    out_dir = args.out or (base / "dataset")
    out_dir.mkdir(parents=True, exist_ok=True)

    rights = load_rights(base)
    if not rights:
        print("AVISO: sin rights_report.json — todo irá a 'revisar'. "
              "Corre antes: python scripts/audit_rights.py <data_dir>")

    index = json.loads((base / "metadata/index.json").read_text(encoding="utf-8"))
    handles: dict[str, object] = {}
    counts: Counter = Counter()
    words: Counter = Counter()
    skipped = Counter()

    for doc_id in index:
        meta_f = base / f"metadata/{doc_id}.json"
        if not meta_f.exists():
            continue
        meta = json.loads(meta_f.read_text(encoding="utf-8"))

        if not meta.get("markdown_file"):
            skipped["sin_texto"] += 1          # p.ej. PDF pendiente de OCR
            continue
        md_f = base / meta["markdown_file"]
        if not md_f.exists():
            skipped["markdown_faltante"] += 1
            continue
        text = body_of(md_f.read_text(encoding="utf-8"))
        n_words = meta.get("word_count") or len(text.split())
        if n_words < args.min_words:
            skipped["texto_corto"] += 1
            continue

        host = urlparse(meta.get("url") or "").netloc.lower()
        part = rights.get(host, DEFAULT_PARTITION)
        if args.only and part not in args.only:
            skipped[f"fuera_de_--only:{part}"] += 1
            continue

        extra = meta.get("extra") or {}
        record = {
            "id": doc_id,
            "text": text,
            "title": meta.get("title"),
            "url": meta.get("url"),
            "domain": host,
            "language": meta.get("language"),
            "category": meta.get("category"),
            "source_type": meta.get("source_type"),
            "published_date": meta.get("published_date"),
            "word_count": n_words,
            # procedencia: por qué este documento está en el corpus
            "provenance": {
                "seed_context": extra.get("seed_context"),
                "crawl_path": meta.get("crawl_path"),
                "depth": meta.get("depth"),
                "fetch_method": meta.get("fetch_method"),
                "extracted_at": meta.get("extracted_at"),
                "ocr_strategy": (meta.get("ocr_signals") or {}).get("strategy_used"),
                "duplicate_urls": extra.get("duplicate_urls"),
            },
            "rights": {
                "partition": part,
                "declared": extra.get("rights"),
            },
        }

        if part not in handles:
            handles[part] = open(out_dir / f"{part}.jsonl", "w", encoding="utf-8")
        handles[part].write(json.dumps(record, ensure_ascii=False) + "\n")
        counts[part] += 1
        words[part] += n_words

    for fh in handles.values():
        fh.close()

    manifest = {
        "generado": datetime.now(timezone.utc).isoformat(),
        "corpus_origen": str(base),
        "min_words": args.min_words,
        "solo_particiones": args.only,
        "particiones": {p: {"documentos": counts[p], "palabras": words[p],
                            "archivo": f"{p}.jsonl",
                            "descripcion": PARTITION_DOC.get(p, "")}
                        for p in sorted(counts)},
        "totales": {"documentos": sum(counts.values()),
                    "palabras": sum(words.values())},
        "descartados": dict(skipped),
        "aviso": ("Las particiones vienen de la clasificación heurística de "
                  "audit_rights.py, que es un dossier de evidencia y NO "
                  "asesoría legal. 'reservado_ia' y 'derechos_reservados' "
                  "quedan apartados a propósito."),
    }
    (out_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    readme = ["# Dataset exportado\n",
              f"Origen: `{base}` · generado {manifest['generado'][:19]}Z\n",
              "Un documento por línea (JSONL) con `text`, metadata, "
              "`provenance` (cómo se llegó a él) y `rights`.\n",
              "## Particiones\n"]
    for p in sorted(counts):
        readme.append(f"- **`{p}.jsonl`** — {counts[p]} docs, "
                      f"{words[p]:,} palabras. {PARTITION_DOC.get(p, '')}\n")
    readme.append("\n" + manifest["aviso"] + "\n")
    (out_dir / "README.md").write_text("".join(readme), encoding="utf-8")

    print(f"Dataset en {out_dir}")
    for p in sorted(counts):
        print(f"  {p:22s} {counts[p]:5d} docs  {words[p]:>10,} palabras")
    print(f"  {'TOTAL':22s} {sum(counts.values()):5d} docs  "
          f"{sum(words.values()):>10,} palabras")
    if skipped:
        print(f"  descartados: {dict(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
