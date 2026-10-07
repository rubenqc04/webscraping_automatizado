"""Libro de visitas: cada URL que el crawl VIO y qué decidió sobre ella.

Por qué existe: el corpus guarda lo que se EXTRAJO, y el mapa de análisis
lo que se ANALIZÓ; ninguno dice qué se descartó ni por qué. Sin eso la
cobertura del pipeline no se puede medir: "¿llegó a todas las páginas de
contenido del sitio?" exige saber tanto lo que se guardó como lo que se
vio y se dejó pasar, y la razón. `scripts/benchmark_coverage.py` cruza
este libro con un inventario independiente del sitio (sitemap, rastreo
de referencia con wget, API) y calcula recall/precisión.

Formato: JSONL append-only en <base_dir>/_ledger/visits.jsonl, una línea
por decisión: {ts, seed, url, decision, reason?, via?, kind?}. Es
thread-safe (los workers del modo paralelo comparten el store y, por lo
tanto, este libro).

Vocabulario de `decision` (cerrado; el benchmark depende de él):

  descubrimiento
    enqueued        la URL entró a la frontier (kind = cómo se descubrió:
                    seed, sitemap, listing_item, pagination, bfs, ...)
    rejected        vista pero no encolada (reason: offsite | depth | excluded)
  resultado del fetch
    saved           se extrajo y guardó un documento
    duplicate       se extrajo, pero su texto ya existía bajo otra URL (alias)
    listing         era un listado: se expandieron sus ítems, no es documento
    hub             portadilla (casi puro enlace), descartada
    no_document     HTML sin documento válido (pocas palabras, extractor vacío)
    blocked         captcha / 403 / 429: el sitio dijo no
    http_error      4xx/5xx: la respuesta es una página de error, no contenido
    robots          robots.txt lo prohíbe
    throttle        el dominio está bloqueado o agotó su cupo de requests
    already_saved   ya estaba en el corpus (dedup por doc_id)
    too_large       binario sobre max_size_mb
    empty           respuesta sin cuerpo útil
    offsite_html    se esperaba un documento y llegó HTML de otro dominio
    extract_error   excepción del extractor
  cierre
    not_fetched     quedó en la frontier al terminar (reason: por qué cerró
                    el crawl: presupuesto, saturación, tope de listados)
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

DECISIONS = frozenset({
    "enqueued", "rejected",
    "saved", "duplicate", "listing", "hub", "no_document", "blocked", "robots",
    "http_error", "throttle",
    "already_saved", "too_large", "empty", "offsite_html", "extract_error",
    "not_fetched",
})


class VisitLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = None

    def record(self, url: str, decision: str, *, seed: str | None = None,
               reason: str | None = None, via: str | None = None,
               kind: str | None = None) -> None:
        if decision not in DECISIONS:
            raise ValueError(f"decisión desconocida: {decision}")
        row = {"ts": round(time.time(), 3), "seed": seed, "url": url,
               "decision": decision}
        if reason:
            row["reason"] = reason
        if via:
            row["via"] = via
        if kind:
            row["kind"] = kind
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self._lock:
            if self._fh is None:
                self._fh = self.path.open("a", encoding="utf-8")
            self._fh.write(line)
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


def read_ledger(path: Path) -> list[dict]:
    """Lee el JSONL completo (tolerante a una última línea truncada)."""
    rows: list[dict] = []
    p = Path(path)
    if not p.exists():
        return rows
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows
