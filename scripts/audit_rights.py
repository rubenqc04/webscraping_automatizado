#!/usr/bin/env python3
"""Auditoría de derechos de uso por dominio de un corpus recolectado.

Para cada dominio presente en el corpus, junta la EVIDENCIA disponible
sobre términos de uso, copyright y reservas frente a minería/IA:

  A. Señales machine-readable (las más fiables):
     - robots.txt: líneas Content-Signal (search/ai-input/ai-train),
       y qué bots de entrenamiento de IA conocidos están bloqueados
       (GPTBot, CCBot, ClaudeBot, Google-Extended...) — bloquearlos es
       una expresión de intención aunque nuestro UA no sea uno de ellos.
     - /.well-known/tdmrep.json (TDM Reservation Protocol, art. 4 DSM UE).
     - <meta name="robots" content="noai/noimageai">, X-Robots-Tag.
     - Licencias declaradas: <link/a rel="license">, license en JSON-LD,
       enlaces a creativecommons.org, meta dc.rights / copyright.
  B. Evidencia humana:
     - Declaración © del pie de página del home.
     - Páginas legales descubiertas en el home ("términos y condiciones",
       "aviso legal", "políticas de uso"...) — se descargan y se buscan
       frases clave ("todos los derechos reservados", "prohibida la
       reproducción", "autorización previa", "creative commons",
       "uso no comercial", "minería de datos", "inteligencia artificial").

Salida: <data_dir>/rights_report.json + tabla resumen por dominio, con
una clasificación heurística preliminar:

    cc-license | tdm-o-ai-reservado | todos-los-derechos-reservados |
    sector-publico | sin-declaracion-visible

ESTO ES UN DOSSIER DE EVIDENCIA, NO ASESORÍA LEGAL: la decisión de
inclusión en un corpus la toma el equipo (idealmente con revisión legal),
usando estas señales y los snippets citados como punto de partida.

Uso:
    python scripts/audit_rights.py <data_dir> [--max-domains N] [--delay 2.0]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Bots de entrenamiento/uso IA cuyo bloqueo en robots.txt expresa intención
AI_BOTS = ["GPTBot", "CCBot", "ClaudeBot", "anthropic-ai", "Google-Extended",
           "Bytespider", "PerplexityBot", "Applebot-Extended",
           "meta-externalagent", "cohere-ai", "Amazonbot", "Diffbot"]

LEGAL_LINK_PAT = re.compile(
    r"t[ée]rminos|condiciones\s+de\s+uso|aviso\s+legal|nota\s+legal"
    r"|pol[íi]ticas?\s+(de\s+)?(uso|privacidad)|copyright|derechos\s+de\s+autor"
    r"|propiedad\s+intelectual|licencia|terms\s+of|legal\s+notice", re.I)

KEY_PHRASES = [
    "todos los derechos reservados", "all rights reserved",
    "prohibida su reproducción", "prohibida la reproducción",
    "queda prohibida", "sin autorización", "autorización previa",
    "autorización escrita", "creative commons", "cc by", "cc-by",
    "dominio público", "uso no comercial", "fines comerciales",
    "minería de datos", "text and data mining", "inteligencia artificial",
    "entrenamiento de modelos", "propiedad intelectual",
    "indecopi", "derecho de autor",
]

COPYRIGHT_LINE_PAT = re.compile(
    r"(©|\(c\)|copyright)\s?[^.\n]{0,120}", re.I)

PUBLIC_SECTOR_PAT = re.compile(r"\.(gob|gov|gub)\.[a-z]{2}$|\.gob$|\.gov$", re.I)


def fetch(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        r = client.get(url)
        return r if r.status_code < 400 else None
    except Exception:
        return None


def robots_signals(client: httpx.Client, base: str) -> dict:
    out = {"content_signal": None, "ai_bots_blocked": [], "available": False}
    r = fetch(client, f"{base}/robots.txt")
    if r is None or "html" in r.headers.get("content-type", ""):
        return out
    text = r.text
    out["available"] = True
    m = re.search(r"^Content-Signal:\s*(.+)$", text, re.M | re.I)
    if m:
        out["content_signal"] = m.group(1).strip()
    # bloques por user-agent: bot -> ¿Disallow: / ?
    blocks = re.split(r"(?im)^user-agent:\s*", text)[1:]
    for block in blocks:
        lines = block.splitlines()
        agent = lines[0].strip()
        rest = "\n".join(lines[1:])
        for bot in AI_BOTS:
            if agent.lower() == bot.lower() and re.search(
                    r"(?im)^disallow:\s*/\s*$", rest):
                out["ai_bots_blocked"].append(bot)
    return out


def tdmrep_signal(client: httpx.Client, base: str) -> dict | None:
    r = fetch(client, f"{base}/.well-known/tdmrep.json")
    if r is None:
        return None
    try:
        return {"found": True, "policy": r.json()}
    except Exception:
        return None


def homepage_signals(client: httpx.Client, base: str) -> dict:
    out = {"meta_noai": False, "license_links": [], "copyright_statement": None,
           "legal_pages": [], "reachable": False}
    r = fetch(client, base)
    if r is None or "html" not in r.headers.get("content-type", ""):
        return out
    out["reachable"] = True
    if "noai" in (r.headers.get("x-robots-tag") or "").lower():
        out["meta_noai"] = True
    soup = BeautifulSoup(r.text, "lxml")

    for meta in soup.find_all("meta", attrs={"name": re.compile("robots", re.I)}):
        if "noai" in (meta.get("content") or "").lower():
            out["meta_noai"] = True
    # licencias declaradas
    for el in soup.find_all(["link", "a"], rel=True):
        rels = el.get("rel")
        if rels and "license" in [str(x).lower() for x in rels] and el.get("href"):
            out["license_links"].append(urljoin(base, el["href"]))
    for a in soup.find_all("a", href=True):
        if "creativecommons.org" in a["href"]:
            out["license_links"].append(a["href"])
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and it.get("license"):
                out["license_links"].append(str(it["license"]))
    out["license_links"] = list(dict.fromkeys(out["license_links"]))[:5]

    # línea de copyright (footer o donde aparezca primero)
    text = " ".join(soup.get_text(" ").split())
    m = COPYRIGHT_LINE_PAT.search(text)
    if m:
        out["copyright_statement"] = m.group(0).strip()[:160]

    # enlaces a páginas legales
    seen = set()
    for a in soup.find_all("a", href=True):
        label = " ".join(a.get_text(" ").split())
        if not label or not LEGAL_LINK_PAT.search(label):
            continue
        url = urljoin(base, a["href"])
        if urlparse(url).netloc != urlparse(base).netloc or url in seen:
            continue
        seen.add(url)
        out["legal_pages"].append({"label": label[:60], "url": url})
        if len(out["legal_pages"]) >= 3:
            break
    return out


def scan_legal_page(client: httpx.Client, page: dict) -> None:
    r = fetch(client, page["url"])
    if r is None:
        page["fetched"] = False
        return
    page["fetched"] = True
    soup = BeautifulSoup(r.text, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = " ".join(soup.get_text(" ").split())
    low = text.lower()
    hits = []
    for phrase in KEY_PHRASES:
        idx = low.find(phrase)
        if idx >= 0:
            snippet = text[max(0, idx - 80): idx + len(phrase) + 120]
            hits.append({"frase": phrase, "evidencia": f"...{snippet}..."})
    page["frases_clave"] = hits


def assess(domain: str, robots: dict, tdm, home: dict, legal_hits: list) -> str:
    cs = (robots.get("content_signal") or "").lower()
    if tdm or "ai-train=no" in cs or "ai=n" in cs or home["meta_noai"]:
        return "tdm-o-ai-reservado"
    if robots["ai_bots_blocked"]:
        return "tdm-o-ai-reservado (bloquea bots de IA)"
    if any("creativecommons" in l for l in home["license_links"]) or \
            any(h["frase"].startswith(("creative commons", "cc by", "cc-by"))
                for h in legal_hits):
        return "cc-license"
    reserved = ("todos los derechos reservados", "all rights reserved",
                "prohibida su reproducción", "prohibida la reproducción",
                "queda prohibida", "autorización previa", "autorización escrita")
    if any(h["frase"] in reserved for h in legal_hits) or \
            (home["copyright_statement"] and
             re.search(r"derechos reservados|rights reserved",
                       home["copyright_statement"], re.I)):
        return "todos-los-derechos-reservados"
    if PUBLIC_SECTOR_PAT.search(domain):
        return "sector-publico (revisar política de datos abiertos)"
    if home["copyright_statement"]:
        return "copyright-declarado-sin-terminos-visibles"
    return "sin-declaracion-visible"


def corpus_domains(data_dir: Path) -> Counter:
    """dominio -> nº de documentos (usa la URL real; los PDFs no traen domain)."""
    domains: Counter = Counter()
    index = json.loads((data_dir / "metadata/index.json").read_text(encoding="utf-8"))
    for entry in index.values():
        host = urlparse(entry.get("url") or "").netloc
        if host:
            domains[host] += 1
    return domains


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=Path,
                    help="corpus a auditar; con --from-csv, directorio de salida")
    ap.add_argument("--from-csv", type=Path, default=None,
                    help="auditar los dominios de un CSV de semillas (columna URL) "
                         "en vez de un corpus ya recolectado")
    ap.add_argument("--max-domains", type=int, default=None)
    ap.add_argument("--delay", type=float, default=2.0)
    args = ap.parse_args()

    if args.from_csv:
        import csv as _csv
        domains = Counter()
        with open(args.from_csv, encoding="utf-8-sig") as fh:
            for row in _csv.DictReader(fh):
                host = urlparse((row.get("URL") or row.get("url") or "").strip()).netloc
                if host:
                    domains[host] += 1
        args.data_dir.mkdir(parents=True, exist_ok=True)
    else:
        domains = corpus_domains(args.data_dir)
    todo = domains.most_common(args.max_domains)
    print(f"Dominios a auditar: {len(todo)} (delay {args.delay}s entre requests)\n")

    client = httpx.Client(headers={"User-Agent": UA}, timeout=20,
                          follow_redirects=True)
    report = []
    for i, (domain, ndocs) in enumerate(todo, 1):
        base = f"https://{domain}"
        robots = robots_signals(client, base)
        time.sleep(args.delay)
        tdm = tdmrep_signal(client, base)
        time.sleep(args.delay)
        home = homepage_signals(client, base)
        legal_hits = []
        for page in home["legal_pages"]:
            time.sleep(args.delay)
            scan_legal_page(client, page)
            legal_hits.extend(page.get("frases_clave") or [])
        verdict = assess(domain, robots, tdm, home, legal_hits)
        report.append({
            "domain": domain, "docs_en_corpus": ndocs,
            "clasificacion_preliminar": verdict,
            "robots": robots, "tdmrep": tdm,
            "home": home,
        })
        print(f"[{i}/{len(todo)}] {domain} ({ndocs} docs) -> {verdict}")
        time.sleep(args.delay)

    out = args.data_dir / "rights_report.json"
    out.write_text(json.dumps({
        "aviso": ("Dossier de evidencia generado automáticamente; NO es "
                  "asesoría legal. La clasificación es heurística y la "
                  "decisión de uso corresponde al equipo/revisión legal."),
        "por_clasificacion": dict(Counter(
            r["clasificacion_preliminar"] for r in report).most_common()),
        "dominios": report,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReporte completo: {out}")

    print("\n═══ resumen por clasificación ═══")
    for cls, n in Counter(r["clasificacion_preliminar"] for r in report).most_common():
        docs = sum(r["docs_en_corpus"] for r in report
                   if r["clasificacion_preliminar"] == cls)
        print(f"  {n:3d} dominios / {docs:4d} docs  {cls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
