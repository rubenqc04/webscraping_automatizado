#!/usr/bin/env python3
"""Auditoría automática de calidad del corpus recolectado.

Revisa cada documento (markdown + metadata + pagemap) y marca señales de
sospecha, produciendo una lista priorizada para revisión humana:

  1. near_duplicate     otro doc casi idéntico (Jaccard de 5-shingles >= 0.7)
                        — mismo artículo alcanzado por dos URLs distintas
  2. boilerplate        líneas que se repiten en >=40% de los docs del mismo
                        dominio (menú/footer que la extracción no limpió)
  3. low_coverage       el doc tiene un pagemap con mucho texto en la página
                        pero el markdown extraído captura poco de él
  4. language_mismatch  idioma detectado (perfil de stopwords es/en/pt)
                        distinto del mayoritario de su dominio
  5. generic_title      título vacío, igual al dominio, o compartido por
                        varios docs
  6. linky              markdown dominado por enlaces (extracción de menú)
  7. http_error         el documento salió de una respuesta 4xx/5xx (página
                        de error guardada como si fuera contenido)

Uso:
    python scripts/audit_corpus.py <data_dir> [--json salida.json]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

STOP = {
    "es": {"el", "la", "los", "las", "de", "del", "que", "y", "en", "un",
           "una", "por", "con", "para", "se", "su", "al", "como", "más"},
    "en": {"the", "of", "and", "to", "in", "a", "is", "that", "for", "on",
           "with", "as", "by", "at", "from", "this", "are", "was"},
    "pt": {"o", "os", "da", "das", "do", "dos", "em", "um", "uma", "não",
           "com", "para", "por", "mais", "como", "seu", "sua", "ao"},
}


def detect_lang(text: str) -> str:
    toks = Counter(re.findall(r"[a-záéíóúñãõç]+", text.lower())[:4000])
    scores = {lang: sum(toks[w] for w in words) for lang, words in STOP.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 5 else "?"


def shingles(text: str, n: int = 5) -> set:
    toks = re.findall(r"\w+", text.lower())
    return {hash(" ".join(toks[i:i + n])) for i in range(len(toks) - n + 1)}


def body_of(md: str) -> str:
    """Quita el front-matter."""
    if md.startswith("---"):
        end = md.find("---", 3)
        if end != -1:
            return md[end + 3:]
    return md


def _strip_boilerplate(base: Path, doc_id: str, doc: dict, common: set) -> int:
    """Remueve del .md las líneas boilerplate del dominio. Devuelve cuántas.

    Conservador: solo coincidencias exactas (línea completa, stripped), el
    front-matter se preserva, y si el cuerpo limpio queda con <30 palabras
    no se toca nada. Idempotente: en una segunda pasada ya no hay comunes.
    """
    meta = doc["meta"]
    if not meta.get("markdown_file"):
        return 0
    md_path = base / meta["markdown_file"]
    full = md_path.read_text(encoding="utf-8")
    front, body = "", full
    if full.startswith("---"):
        end = full.find("---", 3)
        if end != -1:
            front, body = full[:end + 3], full[end + 3:]

    kept, removed = [], 0
    for line in body.splitlines():
        if line.strip() in common:
            removed += 1
        else:
            kept.append(line)
    if not removed:
        return 0
    new_body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if len(new_body.split()) < 30:
        return 0            # quedaría vacío: mejor conservarlo y solo flaggear

    md_path.write_text(front + "\n\n" + new_body + "\n", encoding="utf-8")
    meta_path = base / f"metadata/{doc_id}.json"
    meta["word_count"] = len(new_body.split())
    meta.setdefault("extra", {})["boilerplate_lines_removed"] = \
        meta.get("extra", {}).get("boilerplate_lines_removed", 0) + removed
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    doc["body"] = new_body
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--dup-threshold", type=float, default=0.7)
    ap.add_argument("--fix-boilerplate", action="store_true",
                    help="además de reportar, remueve del markdown las líneas "
                         "boilerplate detectadas (>=40%% de los docs del dominio)")
    args = ap.parse_args()
    base = args.data_dir

    index = json.loads((base / "metadata/index.json").read_text(encoding="utf-8"))
    docs = {}
    for doc_id, entry in index.items():
        meta_f = base / f"metadata/{doc_id}.json"
        if not meta_f.exists():
            continue
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
        body = ""
        if meta.get("markdown_file"):
            md_f = base / meta["markdown_file"]
            if md_f.exists():
                body = body_of(md_f.read_text(encoding="utf-8"))
        docs[doc_id] = {"meta": meta, "body": body}

    flags: dict[str, list] = defaultdict(list)

    # --- 1) casi-duplicados -------------------------------------------------
    sh = {d: shingles(v["body"]) for d, v in docs.items() if len(v["body"]) > 400}
    dup_pairs = []
    for a, b in combinations(sh, 2):
        sa, sb = sh[a], sh[b]
        if not sa or not sb:
            continue
        inter = len(sa & sb)
        if inter and inter / len(sa | sb) >= args.dup_threshold:
            j = round(inter / len(sa | sb), 2)
            dup_pairs.append((a, b, j))
            flags[a].append({"flag": "near_duplicate", "with": b, "jaccard": j})
            flags[b].append({"flag": "near_duplicate", "with": a, "jaccard": j})

    # --- 2) boilerplate por dominio ------------------------------------------
    by_domain = defaultdict(list)
    for d, v in docs.items():
        by_domain[v["meta"].get("domain") or "?"].append(d)
    for domain, ids in by_domain.items():
        if len(ids) < 5:
            continue
        line_freq = Counter()
        for d in ids:
            lines = {l.strip() for l in docs[d]["body"].splitlines()
                     if len(l.strip()) > 25}
            line_freq.update(lines)
        common = {l for l, c in line_freq.items() if c / len(ids) >= 0.4}
        if not common:
            continue
        for d in ids:
            lines = [l.strip() for l in docs[d]["body"].splitlines()
                     if len(l.strip()) > 25]
            if not lines:
                continue
            ratio = sum(1 for l in lines if l in common) / len(lines)
            if ratio > 0.3:
                flags[d].append({"flag": "boilerplate",
                                 "ratio": round(ratio, 2),
                                 "ejemplo": sorted(common)[0][:80]})
            if args.fix_boilerplate and any(l in common for l in lines):
                removed = _strip_boilerplate(base, d, docs[d], common)
                if removed:
                    flags[d].append({"flag": "boilerplate_fixed",
                                     "lineas_removidas": removed})

    # --- 3) cobertura de extracción vs pagemap -------------------------------
    for d, v in docs.items():
        pm_f = base / f"analysis/{d}.pagemap.json"
        if not pm_f.exists() or not v["body"]:
            continue
        pm = json.loads(pm_f.read_text(encoding="utf-8"))
        page_chars = (pm.get("content_root") or {}).get("text_chars") or 0
        if pm.get("page_kind") != "article" or page_chars < 1500:
            continue
        got = len(" ".join(v["body"].split()))
        coverage = got / page_chars
        if coverage < 0.35:
            flags[d].append({"flag": "low_coverage",
                             "extraido_chars": got, "pagina_chars": page_chars,
                             "coverage": round(coverage, 2)})

    # --- 4) idioma vs dominio -------------------------------------------------
    dom_lang: dict[str, str] = {}
    doc_lang: dict[str, str] = {}
    for domain, ids in by_domain.items():
        langs = Counter()
        for d in ids:
            if len(docs[d]["body"]) > 300:
                doc_lang[d] = detect_lang(docs[d]["body"])
                langs[doc_lang[d]] += 1
        if langs:
            dom_lang[domain] = langs.most_common(1)[0][0]
    for d, lang in doc_lang.items():
        expected = dom_lang.get(docs[d]["meta"].get("domain") or "?")
        if expected and lang != "?" and expected != "?" and lang != expected:
            flags[d].append({"flag": "language_mismatch",
                             "detectado": lang, "dominio": expected})

    # --- 5) títulos genéricos / duplicados ------------------------------------
    titles = Counter((v["meta"].get("title") or "").strip() for v in docs.values())
    for d, v in docs.items():
        t = (v["meta"].get("title") or "").strip()
        domain = v["meta"].get("domain") or ""
        if not t or len(t) < 8 or domain.split(".")[-2:-1] == [t.lower()]:
            flags[d].append({"flag": "generic_title", "title": t})
        elif titles[t] > 1:
            flags[d].append({"flag": "duplicated_title", "title": t[:60],
                             "docs_con_este_titulo": titles[t]})

    # --- 6) markdown dominado por enlaces --------------------------------------
    for d, v in docs.items():
        body = v["body"]
        if len(body) < 300:
            continue
        link_chars = sum(len(m) for m in re.findall(r"\[[^\]]*\]\([^)]*\)", body))
        if link_chars / len(body) > 0.5:
            flags[d].append({"flag": "linky",
                             "ratio_enlaces": round(link_chars / len(body), 2)})

    # --- 7) documentos nacidos de una respuesta de error -----------------------
    # Un 404/500/521 devuelve una página ("Página no encontrada", "521: Web
    # server is down") que el extractor tomaba por contenido. El pipeline ya
    # los rechaza; esta señal encuentra los que quedaron en corpus anteriores.
    for d, v in docs.items():
        st = (v.get("meta") or {}).get("http_status")
        if isinstance(st, int) and st >= 400:
            flags[d].append({"flag": "http_error", "http_status": st})

    # --- reporte ---------------------------------------------------------------
    suspects = sorted(flags.items(), key=lambda kv: -len(kv[1]))
    report = {
        "documentos": len(docs),
        "con_flags": len(flags),
        "limpios": len(docs) - len(flags),
        "por_flag": dict(Counter(f["flag"] for fl in flags.values() for f in fl)),
        "pares_duplicados": [{"a": a, "b": b, "jaccard": j} for a, b, j in dup_pairs],
        "sospechosos": [
            {"doc_id": d, "url": docs[d]["meta"].get("url"),
             "title": (docs[d]["meta"].get("title") or "")[:60],
             "flags": fl}
            for d, fl in suspects
        ],
    }
    out = args.json or (base / "audit_report.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Documentos: {report['documentos']} | con flags: {report['con_flags']} "
          f"| limpios: {report['limpios']}")
    print("Por tipo:", json.dumps(report["por_flag"], ensure_ascii=False))
    for s in report["sospechosos"][:10]:
        print(f"  - {s['doc_id']} [{', '.join(f['flag'] for f in s['flags'])}] {s['title']}")
    print(f"\nReporte completo: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
