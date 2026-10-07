# Guía de lectura del código de WebHarvest

Para entender el scraper leyéndolo por partes, en el orden en que un
documento atraviesa el sistema. Siete sesiones de 30 a 60 minutos. Cada una
dice qué archivos abrir, qué buscar, y un ejercicio corto para comprobar
que se entendió. Los tests son la mejor documentación: cada sesión tiene
los suyos.

Convención: los nombres entre comillas son funciones o clases; búscalos
con "buscar en el archivo", no por número de línea (cambian).

```
                 main.py  ──►  parallel.py  ──►  pipeline.py  (el director de orquesta)
                                                     │
      ┌──────────────┬──────────────┬────────────────┼──────────────┬───────────────┐
      ▼              ▼              ▼                ▼              ▼               ▼
  compliance/    fetchers/      analysis/        discovery/     extractors/     storage/
  robots,        httpx →        page_map:        Frontier,      html / pdf /    store, ledger,
  crawl-delay,   Playwright     ¿qué tipo de     sitemap,       docx →          report
  captcha                       página es?       secciones      Document
                                                     ▲
                                                 planner.py + llm.py (modo auto)
```

Tamaño total: ~4.100 líneas en el paquete, 788 en `pipeline.py`. No hace
falta leerlo todo: el 80 % de las decisiones está en cuatro archivos
(`pipeline.py`, `page_map.py`, `crawler.py`, `policy.py`).

---

## Sesión 1 · El vocabulario: `models.py` (113 líneas)

Todo lo que el sistema produce es un `Document`. Entender sus campos es
entender qué promete el scraper.

- Abre `webharvest/models.py`. Lee `DocumentMetadata` campo por campo:
  `url`, `source_type`, `word_count`, `depth`, `crawl_path`, `extra`.
- `make_doc_id`: el identificador es un hash de la URL. Consecuencia: la
  misma URL nunca se guarda dos veces; dos URLs distintas con el mismo
  texto sí (eso lo resuelve el store, sesión 6).
- `count_words`: fíjate por qué no es un `split()` simple (japonés/chino).

**Ejercicio.** Abre un `metadata/<doc_id>.json` de `data_prod/autorizadas_v2`
y reconoce cada campo. Mira `crawl_path`: es el camino desde la semilla.

---

## Sesión 2 · Las reglas antes que nada: `compliance/policy.py` (224 líneas)

Antes de descargar, el sistema pregunta si puede. Este módulo es corto y
define el carácter del proyecto: sin evasión.

- `RobotsPolicy.can_fetch` y `crawl_delay`: lee robots.txt con `protego`
  (comodines). Nota el caché por dominio.
- `DomainThrottle.wait_turn`: el espaciado entre peticiones a un mismo
  sitio, y el cupo `max_requests_per_domain`. Devuelve `False` cuando el
  dominio está bloqueado o agotó su cupo: ese `False` es lo que el
  pipeline anota como `throttle`.
- `BlockDetector.check`: cómo se detecta un captcha. Lee el comentario
  sobre "seres humanos": es el caso real que obligó a exigir palabras
  completas y páginas cortas.

**Ejercicio.** Corre `python -m unittest tests.test_block_detector -v` y
lee cada test: son cinco casos que dicen exactamente qué es y qué no es
un muro anti-bot.

---

## Sesión 3 · Obtener la página: `fetchers/fetchers.py` (393 líneas)

Dos formas de bajar una página y la lógica que decide cuál.

- `StaticFetcher.fetch`: httpx, barato. Devuelve un `FetchResult`; mira
  sus propiedades `is_pdf` e `is_word` (deciden el extractor).
- `needs_dynamic_rendering`: las cuatro señales de que el HTML es un
  cascarón que necesita JavaScript. Lee los comentarios: cada señal nació
  de un sitio concreto (Europe PMC, DSpace).
- `DynamicFetcher.fetch`: Playwright. Fíjate en `_wait_text_stability`
  (espera a que el texto deje de crecer) y `_probe_pdf_actions` (botones
  "Descargar PDF" sin href: hace clic y captura los bytes).
- `AdaptiveFetcher.fetch`: la escalera. Estático primero; dinámico solo si
  hace falta.

**Ejercicio.** `tests/test_multisite_fixtures.py` carga HTML real
capturado de once sitios y afirma cuál necesita navegador. Cambia un
umbral en `needs_dynamic_rendering` y mira qué test se rompe.

---

## Sesión 4 · Entender la página: `analysis/page_map.py` (524 líneas)

El corazón "cognitivo". Dada una página, responde: ¿es un listado, un
artículo o navegación? ¿Dónde está el texto? ¿Cuáles son sus ítems?
¿Cómo se pagina?

Léelo en este orden:

1. `PageMap` (el resultado) y `ListingItem`.
2. `_repeated_groups`: busca grupos de hermanos repetidos con la misma
   firma (tag + clases) y los puntúa. Un listado es "muchos hermanos
   parecidos con enlace". Lee la fórmula del `score`.
3. `_dominant_anchor` y `_extract_listing_items`: cuál es EL enlace de
   cada tarjeta. Lee el comentario del recinto que se repetía: es el bug
   que perdía 3 de cada 12 eventos.
4. `synthesize_next_page_urls`: paginación sintetizada (`?page=N+1`,
   `/page/N/`) cuando la página no trae enlaces "siguiente".
5. `build_page_map`: junta todo y decide `page_kind`.

**Ejercicio.** `tests/test_page_map.py`. Toma una URL real, por ejemplo
`https://www.museoregionalaraucania.gob.cl/cartelera/pasados`, y ejecuta:

```python
import httpx
from webharvest.analysis.page_map import build_page_map
url = "https://www.museoregionalaraucania.gob.cl/cartelera/pasados"
pm = build_page_map(url, httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}).text, {})
print(pm.page_kind, len(pm.listing_items), pm.pagination_urls[:3])
```

---

## Sesión 5 · Decidir qué visitar: `discovery/` y `planner.py`

- `discovery/crawler.py` → `Frontier`: la cola de URLs pendientes con
  cinco niveles de prioridad (documentos > ítems de listado > URLs que
  parecen contenido > sitemap > resto). Lee `add` (quién entra y quién
  no: dominio, profundidad, exclusiones) y `next` (quién sale primero).
  `is_excluded` tiene el caso `/cart` vs `/cartelera`.
- `discovery/sections.py` → `discover_sections`: desde una portada,
  reconoce las secciones de contenido en la navegación y descarta las
  utilitarias ("Contacto", "Login") y las de media.
- `planner.py` → `decide_plan`: el modo agéntico. Con lo que la portada
  reveló (tipo, ítems, secciones, sitemap) elige estrategia:
  listing / directed / sections / sitemap / bfs, con razones. Si queda
  en `low_confidence`, `refine_with_llm` consulta al Qwen local
  (`llm.py`, cliente OpenAI-compatible de 82 líneas).

**Ejercicio.** `tests/test_frontier.py` y `tests/test_planner.py`. En el
segundo, cada test es un tipo de portada y la estrategia esperada.

---

## Sesión 6 · El director: `pipeline.py` (788 líneas)

Aquí se conecta todo. No lo leas de arriba abajo; sigue una URL.

1. `run` → `_crawl_site`: elige el modo del config (`classic`, `sections`,
   `auto`).
2. `_crawl_classic`: crea la `Frontier`, mete la semilla (y el sitemap si
   está activo), y hace `while frontier.next(): _process(...)`. Mira la
   condición de salida: presupuesto o **saturación** (`_saturated`: N
   páginas seguidas sin rendir).
3. `_process`: la función más importante del sistema. Es una secuencia de
   decisiones sobre una URL, y cada `return` anota su motivo en el libro
   de visitas (`_note`): ya guardada, robots, throttle, bloqueada, PDF,
   Word, HTML vacío, fuera de dominio, listado, portadilla (hub), sin
   documento, guardado. Después de guardar decide qué enlaces encolar
   según el tipo de página.
4. `_handle_listing`: qué pasa con un listado: encolar ítems y paginación.
5. `_crawl_by_sections` y `_crawl_section_tree`: el modo secciones,
   recursivo, con presupuesto que se reparte por rama.
6. `_crawl_auto`: sondea la portada, llama al planner, y ejecuta con
   overrides temporales del config.
7. `_track` y `_path`: la procedencia. Cada URL recuerda desde dónde se
   descubrió; `crawl_path` se reconstruye caminando hacia atrás.

**Ejercicio.** Abre `data_battery/deep_sections/_ledger/visits.jsonl` y
sigue una URL: verás `enqueued` (con `via` y `kind`) y luego su decisión.
Cada `decision` corresponde a un `return` de `_process`.

---

## Sesión 7 · Extraer y guardar: `extractors/` y `storage/`

- `extractors/html_extractor.py` → `extract_html_document`: cascada con
  trafilatura; `scrape_head_metadata` (og:title, JSON-LD, fecha) y
  `classify` (categoría de contenido por URL y metadatos).
- `extractors/pdf_extractor.py` → `analyze_ocr_need`: el triaje. Mide
  caracteres por página, cobertura de imagen y **calidad de la capa de
  texto** (`_page_wordy_ratio`: detecta OCR previo corrupto tipo
  "8Q TXLQWR"). Si falla, el PDF va a la cola de OCR sin frenar el crawl.
- `storage/store.py` → `DocumentStore.save`: escribe markdown + metadata +
  índice, con dedup por hash de contenido (dos URLs, mismo texto → alias).
  Todo bajo lock porque en paralelo lo comparten varios workers.
- `storage/ledger.py`: el libro de visitas, y `analysis/coverage.py`, que
  lo cruza con un inventario para medir cobertura.

**Ejercicio.** `tests/test_pdf_quality.py` y `tests/test_store_dedup.py`.

---

## Después: la capa de operación

- `parallel.py` → `ParallelHarvester`: un worker por dominio, componentes
  compartidos (store, throttle, robots) inyectados; `--resume` con
  `_progress/completed_domains.json`. Lee el docstring del módulo: explica
  por qué se paraleliza por dominio y no por URL.
- `main.py`: la CLI. `load_seeds_file` acepta CSV con columna de URL y
  conserva el resto como contexto de la semilla.
- `scripts/`: herramientas sobre el corpus ya recolectado. `audit_corpus`
  (calidad), `audit_rights` (derechos), `export_dataset` (particiones),
  `benchmark_coverage` (cobertura), `run_stats` (métricas del proceso).
- `configs/`: un YAML por corrida. Compara `prod_autorizadas_homes_v2.yaml`
  con `deep_sections_v2.yaml`: la diferencia entre 12 % y 80 % de
  cobertura está en esos números.

## Cómo avanzar

Una sesión por vez. Lee el archivo, corre sus tests, haz el ejercicio, y
anota las preguntas: el siguiente paso es revisarlas juntos y, donde el
código no se explique solo, mejorar el comentario o el nombre.
