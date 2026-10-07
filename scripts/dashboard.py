#!/usr/bin/env python3
"""Panel en vivo de una corrida: qué se está procesando, qué se encontró,
qué se guardó y qué falló, leído de los archivos que la corrida ya escribe.

No toca la corrida ni necesita que esté instrumentada aparte: lee, de forma
incremental, lo que el pipeline ya deja en cada corpus:

    _ledger/visits.jsonl            toda URL vista y la decisión tomada
    metadata/<doc_id>.json          cada documento guardado: palabras, formato, hora
    markdown/<doc_id>.md            caracteres del texto; tokens ≈ caracteres / 4

Documentos, palabras y tokens se cuentan desde metadata/, no desde las
entradas "saved" del libro: una misma noticia publicada en varios sitios
hermanos queda guardada una sola vez, y una URL puede registrarse "saved"
dos veces; el libro sobrecuenta lo que de verdad hay en disco.
    _progress/completed_domains.json dominios terminados
    analysis/<seed>-provenance...   por qué se cerró cada sitio
    ocr_pending/ocr_queue.json      PDFs esperando OCR

Lee el libro de visitas desde donde quedó la última vez (no lo relee entero),
así que funciona igual con mil líneas que con millones.

Uso:
    python scripts/dashboard.py <data_dir> [<data_dir> ...] [--seeds a.csv b.csv] [--port 8765]

y abrir http://localhost:8765 . Desde VS Code remoto, el puerto se reenvía solo
(pestaña PORTS); por SSH: ssh -L 8765:localhost:8765 <nodo>.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

FETCHED = {"saved", "duplicate", "listing", "hub", "no_document", "blocked",
           "robots", "http_error", "throttle", "too_large", "empty",
           "offsite_html", "extract_error", "already_saved"}
FAILURES = {"blocked", "http_error", "extract_error", "too_large", "empty",
            "offsite_html", "robots"}


def host(u: str) -> str:
    return urlparse(u or "").netloc.lower().removeprefix("www.")


class Corpus:
    """Estado agregado de un directorio de corpus, actualizado por incrementos."""

    def __init__(self, path: Path, label: str):
        self.path, self.label = path, label
        self.ledger = path / "_ledger" / "visits.jsonl"
        self.offset = 0
        self.partial = b""
        self.lock = threading.Lock()
        self.totals = Counter()
        self.dom = defaultdict(lambda: {
            "seen": 0, "fetched": 0, "saved": 0, "words": 0, "tokens": 0, "dup": 0, "fail": 0,
            "listing": 0, "discarded": 0, "pdf": 0, "html": 0, "docx": 0,
            "first": 0.0, "last_doc": 0.0, "last": 0.0, "last_url": "", "last_decision": "", "seeds": set(),
            "fail_reasons": Counter(), "closed": None, "seed_close": ""})
        self.events = deque(maxlen=120)
        self.failures = deque(maxlen=200)
        self.saves_per_min = Counter()
        self.docs_total = 0
        self.words_total = 0
        self.tokens_total = 0
        self.meta_seen: set[str] = set()
        self.tokens_per_min = Counter()
        self.types = Counter()
        self.first_ts = None
        self.last_ts = None

    # ------------------------------------------------------------------
    def _scan_metadata(self) -> None:
        """Suma los documentos nuevos de metadata/ (solo los no vistos)."""
        try:
            names = [e.name for e in os.scandir(self.path / "metadata")
                     if e.name.endswith(".json") and e.name != "index.json"
                     and e.name not in self.meta_seen]
        except FileNotFoundError:
            return
        for name in names:
            try:
                m = json.loads((self.path / "metadata" / name).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue                 # a medio escribir: se reintenta luego
            self.meta_seen.add(name)
            chars = 0
            if m.get("markdown_file"):
                try:
                    chars = len((self.path / m["markdown_file"]).read_text(
                        encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
            tokens = chars // 4          # convención del equipo
            words = int(m.get("word_count") or 0)
            cp = m.get("crawl_path") or []
            seed = cp[0].get("url") if cp and isinstance(cp[0], dict) else ""
            dk = host(seed) or (m.get("domain") or "").lower().removeprefix("www.")
            try:
                ts = datetime.fromisoformat(m["extracted_at"]).timestamp()
            except (KeyError, TypeError, ValueError):
                ts = time.time()
            st = m.get("source_type") or ""
            kind = "pdf" if st == "pdf" else "docx" if st == "docx" else "html"
            D = self.dom[dk]
            D["saved"] += 1
            D["words"] += words
            D["tokens"] += tokens
            D[kind] += 1
            D["first"] = min(D["first"] or ts, ts)
            D["last_doc"] = max(D["last_doc"], ts)
            self.docs_total += 1
            self.words_total += words
            self.tokens_total += tokens
            self.types[kind] += 1
            self.saves_per_min[int(ts // 60)] += 1
            self.tokens_per_min[int(ts // 60)] += tokens

    def update(self) -> None:
        with self.lock:
            self._scan_metadata()
        try:
            size = self.ledger.stat().st_size
        except FileNotFoundError:
            return
        if size < self.offset:          # el archivo se reinició: releer
            self.offset, self.partial = 0, b""
        if size == self.offset:
            return
        with self.ledger.open("rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(size - self.offset)
        self.offset = size
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()       # la última puede venir a medio escribir
        with self.lock:
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    r = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._ingest(r)

    def _ingest(self, r: dict) -> None:
        d, url, seed = r.get("decision"), r.get("url", ""), r.get("seed") or ""
        ts = r.get("ts") or time.time()
        self.first_ts = self.first_ts or ts
        self.last_ts = ts
        dk = host(seed) or host(url)
        D = self.dom[dk]
        D["seeds"].add(seed)
        D["last"], D["last_url"], D["last_decision"] = ts, url, d
        self.totals[d] += 1
        if d == "enqueued":
            D["seen"] += 1
        if d in FETCHED:
            D["fetched"] += 1
        if d == "saved":
            pass                         # el documento se cuenta desde metadata/
        elif d == "duplicate":
            D["dup"] += 1
        elif d == "listing":
            D["listing"] += 1
        elif d in ("hub", "no_document"):
            D["discarded"] += 1
        # la portada misma falló: es el motivo de un sitio que cierra en 0
        if url == seed and d in FAILURES | {"throttle"}:
            D["seed_close"] = d + (f": {r.get('reason')}" if r.get("reason") else "")
        if d in FAILURES:
            D["fail"] += 1
            D["fail_reasons"][d] += 1
            self.failures.appendleft({"ts": ts, "dom": dk, "url": url,
                                      "decision": d, "reason": r.get("reason")})
        if d != "enqueued":
            self.events.appendleft({"ts": ts, "dom": dk, "url": url,
                                    "decision": d, "reason": r.get("reason"),
                                    "corpus": self.label})

    # ------------------------------------------------------------------
    def snapshot(self, now: float, seeds_by_dom: dict) -> dict:
        try:
            done = set(json.loads((self.path / "_progress" /
                                   "completed_domains.json").read_text()))
        except Exception:
            done = set()
        done = {h.removeprefix("www.") for h in done}
        try:
            ocr = len(json.loads((self.path / "ocr_pending" /
                                  "ocr_queue.json").read_text()))
        except Exception:
            ocr = 0
        with self.lock:
            rows = []
            for dk, D in self.dom.items():
                age = now - D["last"]
                status = ("terminado" if dk in done else
                          "activo" if age < 150 else "en pausa")
                rows.append({
                    "dom": dk, "corpus": self.label, "status": status,
                    "seen": D["seen"], "fetched": D["fetched"], "saved": D["saved"],
                    "words": D["words"], "tokens": D["tokens"],
                    # ritmo del dominio entre su primer y último documento;
                    # con menos de 10 min de datos el cociente no dice nada
                    "tps": (round(D["tokens"] / (D["last_doc"] - D["first"]), 1)
                            if D["last_doc"] - D["first"] >= 600 else None),
                    "dup": D["dup"], "fail": D["fail"],
                    "listing": D["listing"], "discarded": D["discarded"],
                    "pdf": D["pdf"], "html": D["html"], "docx": D["docx"],
                    "last_age": round(age), "last_url": D["last_url"],
                    "last_decision": D["last_decision"],
                    "fail_reasons": dict(D["fail_reasons"]),
                    "seed_close": D["seed_close"],
                })
            for dk, inst in seeds_by_dom.get(self.label, {}).items():
                if dk not in self.dom:
                    rows.append({"dom": dk, "corpus": self.label,
                                 "status": "terminado" if dk in done else "pendiente",
                                 "inst": inst, "seen": 0, "fetched": 0, "saved": 0,
                                 "words": 0, "tokens": 0, "tps": 0, "dup": 0, "fail": 0, "listing": 0,
                                 "discarded": 0, "pdf": 0, "html": 0, "docx": 0,
                                 "last_age": None, "last_url": "", "last_decision": "",
                                 "fail_reasons": {}})
            inst_map = seeds_by_dom.get(self.label, {})
            for r in rows:
                r.setdefault("inst", inst_map.get(r["dom"], ""))
            return {
                "label": self.label, "path": str(self.path), "rows": rows,
                "totals": dict(self.totals), "docs": self.docs_total, "words": self.words_total,
                "tokens": self.tokens_total,
                "tokens_per_min": dict(self.tokens_per_min),
                "types": dict(self.types), "ocr_pending": ocr,
                "done": len(done), "first_ts": self.first_ts, "last_ts": self.last_ts,
                "saves_per_min": dict(self.saves_per_min),
                "events": list(self.events)[:60], "failures": list(self.failures)[:80],
            }


def load_seeds(files: list[Path], labels: list[str]) -> dict:
    """{label_del_corpus: {dominio: institución}} para mostrar los pendientes."""
    out: dict[str, dict] = {}
    for f, lab in zip(files, labels):
        m = {}
        with open(f, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                u = (r.get("URL") or r.get("url") or "").strip()
                if u:
                    m.setdefault(host(u if "://" in u else "https://" + u),
                                 (r.get("Institucion") or "").strip())
        out[lab] = m
    return out


def slurm_jobs() -> list[dict]:
    try:
        out = subprocess.run(["squeue", "-u", os.environ.get("USER", ""), "-h", "-o",
                              "%i|%j|%T|%M|%l|%R"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return []
    jobs = []
    for line in out.splitlines():
        p = line.split("|")
        if len(p) == 6:
            jobs.append(dict(zip(["id", "name", "state", "elapsed", "limit", "where"], p)))
    return jobs


def push_loop(space: str, state, every: float) -> None:
    """Publica el estado en un Space estático privado de Hugging Face.

    El Space no ve los archivos del cluster: este proceso calcula el estado
    aquí y lo sube como state.json con un commit (API de commits de HF, sin
    depender de huggingface_hub, que el .venv del proyecto no trae). Cada
    50 commits compacta el historial para que el repo no crezca."""
    import base64
    import urllib.error
    import urllib.request
    token = os.environ.get("HF_TOKEN")
    if not token:
        tf = Path(os.environ.get("WEBHARVEST_HF_TOKEN_FILE") or
                  Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")) / "token")
        token = tf.read_text().strip()
    api = f"https://huggingface.co/api/spaces/{space}"

    def post(url: str, data: bytes, ctype: str) -> None:
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": ctype})
        urllib.request.urlopen(req, timeout=60).close()

    commits, wait = 0, every
    while True:
        t0 = time.time()
        body = state()
        ndjson = "\n".join(json.dumps(x) for x in (
            {"key": "header", "value": {"summary": "estado"}},
            {"key": "file", "value": {"path": "state.json", "encoding": "base64",
                                      "content": base64.b64encode(body).decode()}},
        )).encode()
        try:
            post(f"{api}/commit/main", ndjson, "application/x-ndjson")
            commits += 1
            wait = every
            msg = f"ok · {len(body) // 1024} KB"
            if commits % 50 == 0:
                post(f"{api}/super-squash/main",
                     json.dumps({"message": "panel: historial compactado"}).encode(),
                     "application/json")
                msg += " · historial compactado"
        except urllib.error.HTTPError as e:
            if e.code == 429:            # límite de commits: espaciar los envíos
                wait = min(wait * 2, 1800)
            msg = f"falló: HTTP {e.code} {e.read()[:200]!r}"
        except Exception as e:
            msg = f"falló: {e}"
        print(time.strftime("%H:%M:%S"), msg, flush=True)
        time.sleep(max(1.0, wait - (time.time() - t0)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dirs", nargs="+", type=Path)
    ap.add_argument("--seeds", nargs="*", type=Path, default=[],
                    help="CSV de semillas, uno por data_dir y en el mismo orden")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--push", metavar="ORG/SPACE",
                    help="en vez de servir la página, publicar el estado en este Space "
                         "estático de Hugging Face (p. ej. latam-gpt/webharvest-dashboard)")
    ap.add_argument("--every", type=float, default=120,
                    help="segundos entre envíos con --push (por defecto 120)")
    a = ap.parse_args()

    corpora = [Corpus(p, p.name) for p in a.data_dirs]
    seeds = load_seeds(a.seeds, [c.label for c in corpora]) if a.seeds else {}
    html = (Path(__file__).with_name("dashboard.html")).read_text(encoding="utf-8")
    cache = {"t": 0.0, "body": b"{}"}
    cache_lock = threading.Lock()

    def state() -> bytes:
        with cache_lock:
            if time.time() - cache["t"] < 2:
                return cache["body"]
            for c in corpora:
                c.update()
            now = time.time()
            body = json.dumps({"now": now, "jobs": slurm_jobs(),
                               "corpora": [c.snapshot(now, seeds) for c in corpora]},
                              ensure_ascii=False).encode("utf-8")
            cache.update(t=time.time(), body=body)
            return body

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/api/state"):
                body, ctype = state(), "application/json; charset=utf-8"
            elif self.path in ("/", "/index.html"):
                body, ctype = html.encode("utf-8"), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print("leyendo el estado inicial (la primera lectura recorre el libro completo)…",
          flush=True)
    t0 = time.time()
    state()
    if a.push:
        print(f"listo en {time.time()-t0:.1f}s · publicando en {a.push} cada {a.every:.0f}s",
              flush=True)
        push_loop(a.push, state, a.every)
        return
    print(f"listo en {time.time()-t0:.1f}s · http://{a.host}:{a.port}", flush=True)
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
