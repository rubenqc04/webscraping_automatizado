"""Crawl paralelo por dominio, reanudable.

Por qué por dominio y no por semilla: la cortesía se mide POR SITIO
(crawl-delay de su robots.txt). Si dos workers atacan el mismo dominio se
duplica la carga que ese servidor recibe; si cada worker toma un dominio
distinto, cada sitio sigue viendo exactamente el mismo ritmo que en modo
secuencial, pero el reloj de pared se divide entre N. Con 225 dominios y
delays de 2 s, la corrida secuencial pasa casi todo el tiempo esperando.

Diseño:

    ┌── componentes COMPARTIDOS (con lock interno) ──┐
    │  DocumentStore · DomainThrottle · RobotsPolicy │
    └───────────────┬────────────────────────────────┘
                    │ inyectados
      ┌─────────────┴──────────────┐
      ▼             ▼              ▼
   worker 1      worker 2   ...  worker N     un ScrapePipeline cada uno
   (fetcher      (fetcher        (fetcher     -> su propio navegador y su
    propio)       propio)         propio)        propio estado de crawl

El throttle compartido es además la red de seguridad: si dos dominios
resuelven al mismo host (o un PDF cruza de dominio), el espaciado se
sigue respetando porque el estado por dominio es global.

Reanudabilidad: cada dominio terminado se anota en
`<base_dir>/_progress/completed_domains.json`. Al relanzar con --resume se
saltan los dominios ya hechos; dentro de un dominio, la deduplicación por
doc_id del store evita re-descargar lo ya guardado.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from .compliance.policy import DomainThrottle, RobotsPolicy
from .pipeline import ScrapePipeline
from .storage.store import DocumentStore

log = logging.getLogger("webharvest.parallel")


class ParallelHarvester:
    def __init__(self, cfg: dict, workers: int = 4, resume: bool = False):
        self.cfg = cfg
        self.workers = max(1, workers)
        self.resume = resume

        # compartidos entre todos los workers
        self.robots = RobotsPolicy(
            user_agent=cfg["identity"]["user_agent"],
            respect=cfg["compliance"]["respect_robots_txt"],
            default_delay=cfg["compliance"]["default_crawl_delay"],
        )
        self.throttle = DomainThrottle(
            self.robots, cfg["compliance"]["max_requests_per_domain"]
        )
        self.store = DocumentStore(cfg["storage"])

        self._progress_dir = Path(cfg["storage"]["base_dir"]) / "_progress"
        self._progress_dir.mkdir(parents=True, exist_ok=True)
        self._progress_file = self._progress_dir / "completed_domains.json"
        self._progress_lock = threading.Lock()
        self._done: set[str] = set()
        if self.resume and self._progress_file.exists():
            try:
                self._done = set(json.loads(
                    self._progress_file.read_text(encoding="utf-8")))
                log.info("Reanudando: %d dominios ya completados se omiten",
                         len(self._done))
            except json.JSONDecodeError:
                log.warning("Progreso corrupto en %s; se ignora", self._progress_file)

    # ------------------------------------------------------------------
    def _mark_done(self, domain: str) -> None:
        with self._progress_lock:
            self._done.add(domain)
            tmp = self._progress_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self._done), ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            tmp.replace(self._progress_file)

    @staticmethod
    def group_by_domain(seeds: list[str]) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for url in seeds:
            host = urlparse(url).netloc.lower()
            if host:
                groups[host].append(url)
        # dominios con más semillas primero: los "gordos" arrancan temprano y
        # no quedan de cola bloqueando el final de la corrida
        return dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))

    # ------------------------------------------------------------------
    def _run_domain(self, domain: str, urls: list[str],
                    seed_context: dict) -> tuple[str, str | None]:
        """Un worker: pipeline propio para todas las semillas de un dominio.

        No se miden documentos por diferencia del total del store: bajo
        concurrencia ese delta incluye lo que guardan los demás workers. El
        recuento por dominio se hace al final, leyendo el índice.
        """
        pipeline = ScrapePipeline(self.cfg, store=self.store,
                                  throttle=self.throttle, robots=self.robots)
        error = None
        try:
            pipeline.run(urls, seed_context=seed_context)
        except Exception as exc:                      # nunca tumbar la corrida
            error = f"{type(exc).__name__}: {exc}"
            log.exception("Dominio %s falló", domain)
        finally:
            pipeline.close()
        if error is None:
            self._mark_done(domain)
        return domain, error

    def _docs_per_host(self) -> dict[str, int]:
        """Documentos por host, leídos del índice (fuente de verdad)."""
        counts: dict[str, int] = defaultdict(int)
        index_path = Path(self.cfg["storage"]["base_dir"]) / \
            self.cfg["storage"]["master_index"]
        if not index_path.exists():
            return {}
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        for entry in index.values():
            host = urlparse(entry.get("url") or "").netloc.lower()
            if host:
                counts[host] += 1
        return dict(counts)

    # ------------------------------------------------------------------
    def run(self, seeds: list[str],
            seed_context: dict[str, dict] | None = None) -> dict:
        groups = self.group_by_domain(seeds)
        pending = {d: u for d, u in groups.items() if d not in self._done}
        skipped = len(groups) - len(pending)
        log.info("Paralelo: %d dominios (%d omitidos por --resume), "
                 "%d semillas, %d workers",
                 len(pending), skipped, sum(len(u) for u in pending.values()),
                 self.workers)

        finished, errors = [], []
        with ThreadPoolExecutor(max_workers=self.workers,
                                thread_name_prefix="harvest") as pool:
            futures = {pool.submit(self._run_domain, d, u, seed_context or {}): d
                       for d, u in pending.items()}
            for i, fut in enumerate(as_completed(futures), 1):
                domain, error = fut.result()
                finished.append(domain)
                if error:
                    errors.append({"domain": domain, "error": error})
                log.info("[%d/%d] %s terminado%s", i, len(futures), domain,
                         f" (ERROR: {error})" if error else "")

        # recuento fiable, ya sin concurrencia
        per_host = self._docs_per_host()
        err_by_domain = {e["domain"]: e["error"] for e in errors}
        results = [{"domain": d, "documents": per_host.get(d, 0),
                    "error": err_by_domain.get(d)} for d in finished]
        for r in sorted(results, key=lambda r: -r["documents"])[:10]:
            log.info("  %s: %d documentos", r["domain"], r["documents"])

        stats = self.store.stats()
        stats.update({
            "blocked_domains": self.throttle.blocked_domains(),
            "domains_crawled": len(results),
            "domains_skipped_resume": skipped,
            "domains_with_errors": len(errors),
            "workers": self.workers,
        })
        (self._progress_dir / "last_run_domains.json").write_text(
            json.dumps(sorted(results, key=lambda r: -r["documents"]),
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return stats
