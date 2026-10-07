# WebHarvest — scraping adaptativo de fuentes autorizadas

WebHarvest recolecta texto en español de sitios institucionales de
Latinoamérica para entrenar modelos de lenguaje (proyecto LatamGPT, CENIA).
Dada la URL de un sitio, reconoce qué tipo de página tiene delante, recorre el
sitio en profundidad respetando sus límites (robots.txt, espera entre
peticiones, bloqueos), extrae el texto de HTML, PDF y Word, y lo guarda con su
procedencia y un registro de cada URL vista.

Este documento explica **qué hace cada script y en qué paso del pipeline
entra**. La arquitectura y las decisiones de diseño están en
[ARQUITECTURA.md](ARQUITECTURA.md); una lectura guiada del código, en
[GUIA_CODIGO.md](GUIA_CODIGO.md).

---

## El recorrido de una URL

```
 semillas (CSV)
      │
 main.py ──► parallel.py ──► pipeline.py  (un sitio por worker)
                                 │
   ┌─────────────────────────────┼──────────────────────────────────┐
   │ 1 plan        planner.py (+ llm.py si la heurística duda)       │
   │ 2 descubrir   discovery/sections.py · discovery/crawler.py      │
   │ 3 permisos    compliance/policy.py  (robots, turno, bloqueos)   │
   │ 4 descargar   fetchers/fetchers.py  (httpx → Playwright)        │
   │ 5 entender    analysis/page_map.py  (listado, artículo, pág. N) │
   │ 6 extraer     extractors/ html · pdf · docx                     │
   │ 7 guardar     storage/store.py · storage/ledger.py              │
   └─────────────────────────────┬──────────────────────────────────┘
                                 │
 después:  process_ocr_queue.py · scripts/audit_* · scripts/export_dataset.py
 en vivo:  scripts/dashboard.py
```

---

## 1. Punto de entrada

| Script | Qué hace |
|---|---|
| [`main.py`](main.py) | La línea de comandos. Lee el config (`--config`) y las semillas (`--url` o `--url-file`), y lanza el crawl. `--workers N` recorre N sitios en paralelo; `--resume` salta los sitios ya terminados; `--report` y `--ocr-report` generan reportes del corpus. |
| [`webharvest/parallel.py`](webharvest/parallel.py) | Reparte los dominios entre workers (un sitio nunca se recorre en dos workers a la vez, para respetar su ritmo) y anota cada sitio terminado en `_progress/completed_domains.json`, que es lo que hace reanudable una corrida. |
| [`webharvest/pipeline.py`](webharvest/pipeline.py) | **El orquestador.** Para cada sitio elige el modo de recorrido (secciones, clásico o automático), pasa cada URL por los pasos 3 a 7 y decide qué hacer con ella: guardarla, seguir sus enlaces, paginar, descartarla. También aplica los rieles de cierre: saturación (muchas páginas seguidas sin documentos nuevos), presupuesto de páginas y presupuesto de tiempo por sitio. |
| [`webharvest/models.py`](webharvest/models.py) | Las estructuras comunes: el documento extraído, sus metadatos y `make_doc_id`, el identificador estable de cada URL. |

## 2. Plan y descubrimiento

| Script | Qué hace |
|---|---|
| [`webharvest/planner.py`](webharvest/planner.py) | Sondea la portada una vez y decide la estrategia con heurísticas (cuántas secciones tiene, si es un listado, si tiene sitemap). Deja el plan y sus razones en `analysis/`. |
| [`webharvest/llm.py`](webharvest/llm.py) | Árbitro opcional: si la heurística queda en duda, consulta a un modelo local (Qwen2.5-32B vía vLLM). Sin servidor, el pipeline usa solo la heurística. |
| [`webharvest/discovery/sections.py`](webharvest/discovery/sections.py) | Encuentra las secciones de contenido en el menú de la portada (Noticias, Publicaciones, Biblioteca…), para recorrer cada una con su propio presupuesto. |
| [`webharvest/discovery/crawler.py`](webharvest/discovery/crawler.py) | La cola de URLs pendientes (`Frontier`), con cinco niveles de prioridad: documentos, ítems de listado, URLs con pinta de contenido, sitemap y el resto. Define el alcance del sitio (`same_site`: el mismo host sin `www.`, más adonde redirige la portada), lee sitemaps, extrae enlaces y descarta trampas de query y patrones excluidos. |

## 3. Permisos y límites del sitio

| Script | Qué hace |
|---|---|
| [`webharvest/compliance/policy.py`](webharvest/compliance/policy.py) | Todo lo que decide si se puede pedir una página. `RobotsPolicy` lee robots.txt y su `Crawl-delay`. `DomainThrottle` espera entre peticiones al mismo dominio, lleva el cupo por dominio y veta un dominio que nos bloqueó. `BlockDetector` reconoce un bloqueo en la respuesta: códigos 401/403/407/429/503 o una página corta con marcadores de desafío (Cloudflare, reCAPTCHA, hCaptcha, DataDome…). Ante un captcha, un 403 o un 429 el dominio completo se deja de pedir: el "no" del sitio se respeta. |

## 4. Descarga

| Script | Qué hace |
|---|---|
| [`webharvest/fetchers/fetchers.py`](webharvest/fetchers/fetchers.py) | `StaticFetcher` pide la página con httpx. `DynamicFetcher` la renderiza con Playwright (Chromium headless) cuando llega con poco texto y señales de estar armada por JavaScript. `AdaptiveFetcher` combina ambos: primero httpx, el navegador solo si hace falta, y si el navegador falla se queda con lo estático. Una respuesta bloqueada no se reintenta con el navegador. |

## 5. Entender la página

| Script | Qué hace |
|---|---|
| [`webharvest/analysis/page_map.py`](webharvest/analysis/page_map.py) | Clasifica la página sin selectores por sitio: listado (grupos de elementos hermanos repetidos con enlaces), artículo (un bloque dominante de texto), portadilla (casi solo enlaces). En los listados obtiene los ítems y la paginación: 21 convenciones distintas (parámetros `?page=`, `/page/N/`, "Siguiente", números, `rel=next`, y páginas siguientes sintetizadas cuando la paginación es solo JavaScript, validadas porque traigan ítems nuevos). |
| [`webharvest/analysis/coverage.py`](webharvest/analysis/coverage.py) | Mide cobertura: compara el libro de visitas con un inventario independiente del sitio (ver sección 10). |

## 6. Extracción

| Script | Qué hace |
|---|---|
| [`webharvest/extractors/html_extractor.py`](webharvest/extractors/html_extractor.py) | El texto principal de una página HTML (trafilatura), sin menús ni pies, más título, fecha, autores e idioma. |
| [`webharvest/extractors/pdf_extractor.py`](webharvest/extractors/pdf_extractor.py) | El texto de un PDF (pymupdf4llm) y el triaje de OCR: si el PDF es escaneado (poco texto por página o páginas que son casi solo imagen) queda en la cola de OCR en vez de guardarse vacío. |
| [`webharvest/extractors/docx_extractor.py`](webharvest/extractors/docx_extractor.py) | Documentos Word: .docx con mammoth. Los .doc antiguos (Word 97-2003) se detectan pero quedan pendientes: no hay un extractor confiable en Python puro. |

## 7. Almacenamiento y registro

| Script | Qué hace |
|---|---|
| [`webharvest/storage/store.py`](webharvest/storage/store.py) | Escribe cada documento: el texto en `markdown/`, los metadatos en `metadata/` (URL, título, fecha, palabras, cómo se obtuvo, ruta de llegada) y los PDF en `pdfs/`. Descarta duplicados por contenido (el mismo texto en otra URL). |
| [`webharvest/storage/ledger.py`](webharvest/storage/ledger.py) | El libro de visitas `_ledger/visits.jsonl`: cada URL que el crawl vio y qué decidió sobre ella, con un vocabulario cerrado (`saved`, `duplicate`, `listing`, `robots`, `blocked`, `http_error`, `rejected` con motivo…). Es la base del panel en vivo y de la medición de cobertura. |
| [`webharvest/storage/report.py`](webharvest/storage/report.py) | Reporte consolidado del corpus para revisión humana (`main.py --report`). |

## 8. Después del crawl

| Script | Qué hace |
|---|---|
| [`process_ocr_queue.py`](process_ocr_queue.py) | Procesa en diferido los PDF escaneados de la cola de OCR (ocrmypdf/Tesseract u otros motores, `--strategy`), para no frenar el crawl con un paso lento. |
| [`scripts/audit_corpus.py`](scripts/audit_corpus.py) | Auditoría de calidad del texto recolectado. Marca para revisión: documentos casi idénticos entre sí, líneas repetidas en buena parte de los documentos de un dominio (menús, pies), extracciones que capturaron poco del texto de la página e idioma distinto al del resto del dominio. `--fix-boilerplate` quita las líneas repetidas. |
| [`scripts/audit_rights.py`](scripts/audit_rights.py) | Auditoría de derechos por dominio: busca términos de uso y licencias (Creative Commons, "todos los derechos reservados", cláusulas sobre IA) y clasifica cada sitio. |
| [`scripts/export_dataset.py`](scripts/export_dataset.py) | Empaqueta el corpus como dataset JSONL, separado por estado de derechos (permisivo, a revisar, reservado para IA, derechos reservados). |
| [`scripts/run_stats.py`](scripts/run_stats.py) | Métricas operacionales de una corrida a partir de su log. |
| [`scripts/site_tree.py`](scripts/site_tree.py) | Reconstruye el árbol de un sitio desde la procedencia de cada documento (por qué página se llegó a él). |

## 9. Monitoreo en vivo

| Script | Qué hace |
|---|---|
| [`scripts/dashboard.py`](scripts/dashboard.py) + [`dashboard.html`](scripts/dashboard.html) | Panel de una corrida mientras avanza: documentos, tokens (caracteres / 4) y tok/s, estado de cada dominio (activo, en pausa, terminado, pendiente), la URL en proceso, últimos eventos y fallas con su motivo. Lee el libro de visitas y `metadata/` sin tocar la corrida. Local: `--port 8765`. Con `--push ORG/SPACE` publica el estado en un Space estático privado de Hugging Face. |
| [`scripts/deploy_space.py`](scripts/deploy_space.py) | Crea o actualiza ese Space (la página y su README, en [`space/`](space/)). |

## 10. Medición de cobertura y pruebas

| Script | Qué hace |
|---|---|
| [`scripts/reference_crawl.py`](scripts/reference_crawl.py) | Un rastreo independiente y simple (BFS, opcionalmente con navegador) para armar el inventario de URLs de un sitio. |
| [`scripts/benchmark_coverage.py`](scripts/benchmark_coverage.py) | Compara lo que alcanzó el pipeline contra ese inventario, un sitemap o un log de wget: alcance, descarga, recall y precisión. |
| [`scripts/probe_pagination.py`](scripts/probe_pagination.py) | Comprueba que la paginación de un sitio se detecte **y** avance de verdad (que la página 2 traiga ítems nuevos). |
| [`ocr-eval/eval_ocr.py`](ocr-eval/eval_ocr.py) | Compara motores de OCR sobre PDF reales; resultados en `ocr-eval/output/`. |
| [`scripts/compare_api_vs_scraping.py`](scripts/compare_api_vs_scraping.py) | Experimento de referencia: scraping contra la API oficial de Europe PMC. |
| [`tests/`](tests/) | 146 tests (`python -m pytest tests`): bloqueos, paginación, alcance del sitio, presupuestos, extracción, deduplicación, libro de visitas, cobertura. |

## 11. Semillas

| Script | Qué hace |
|---|---|
| [`scripts/parse_links_ordenado.py`](scripts/parse_links_ordenado.py) | Convierte el Excel de fuentes autorizadas (hoja "Links") en los CSV de semillas de la corrida v3: portadas y secciones profundas. |
| [`scripts/parse_authorized_sources.py`](scripts/parse_authorized_sources.py) | La versión anterior, para el primer formato del Excel. |

Las semillas publicadas en [`configs/seeds/`](configs/seeds/) traen solo
institución y URL; el estado de permiso de cada fuente se lleva aparte.

## 12. Ejecución en el cluster (Slurm)

| Script | Qué hace |
|---|---|
| [`slurm/corrida_v3.sbatch`](slurm/corrida_v3.sbatch) | Una corrida completa: `sbatch slurm/corrida_v3.sbatch <config> <semillas.csv> [workers]`. Con `--resume`, relanzar el mismo comando retoma donde quedó. |
| [`slurm/crawl.sbatch`](slurm/crawl.sbatch) | Crawl genérico, solo CPU. |
| [`slurm/crawl_llm.sbatch`](slurm/crawl_llm.sbatch) | Crawl con el árbitro LLM: pide una GPU y levanta vLLM con Qwen2.5-32B. |
| [`slurm/dashboard_push.sbatch`](slurm/dashboard_push.sbatch) | Publica el estado de la corrida en el panel del Space cada 2 minutos. |

Guía de operación: [`slurm/GUIA.md`](slurm/GUIA.md); detalles del cluster:
[`SLURM.md`](SLURM.md).

## Configuración

Cada corrida se define con un YAML en [`configs/`](configs/): modo de
recorrido, presupuestos (páginas por sitio y por sección, segundos por sitio,
ventana de saturación), patrones excluidos, respeto de robots.txt y espera
entre peticiones, y dónde se escribe el corpus. Los vigentes son
`v3_portadas.yaml` y `v3_profundas.yaml`; los `battery_*`, `bench_*`,
`cov_*`, `deep_*` y `pag_*` son de experimentos y mediciones anteriores.

---

## Uso rápido

```bash
pip install -r requirements.txt
playwright install chromium

# un sitio, local
python main.py --config configs/v3_portadas.yaml --url https://www.ejemplo.cl/

# una lista de semillas, 8 sitios en paralelo, reanudable
python main.py --config configs/v3_portadas.yaml \
    --url-file configs/seeds/v3/portadas.csv --workers 8 --resume

# verla avanzar
python scripts/dashboard.py <base_dir del config> --port 8765
```

El corpus queda en el `storage.base_dir` del config:

```
markdown/   el texto de cada documento        metadata/   sus metadatos
pdfs/       los PDF descargados               ocr_pending/ la cola de OCR
analysis/   mapas de página, planes, procedencia
_ledger/visits.jsonl   cada URL vista y la decisión tomada
_progress/  sitios terminados (para --resume)
```
