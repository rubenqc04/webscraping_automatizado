# WebHarvest — scraping adaptativo, estructurado y respetuoso

Framework en Python para recopilar texto relevante (noticias, artículos científicos, blogs, sitios gubernamentales, etc.) desde páginas estáticas o dinámicas, con extracción de PDFs vía `pymupdf4llm`, triaje automático de OCR y almacenamiento relacionado por identificador (`.md` + `.json` + `.pdf`).

> **Para leer el código**: [`GUIA_CODIGO.md`](GUIA_CODIGO.md) lo recorre en siete
> sesiones, siguiendo un documento por el sistema, con los tests y un ejercicio
> por sesión. Este README explica el **por qué** de cada decisión de diseño.
> **Para correr en el cluster**: [`slurm/GUIA.md`](slurm/GUIA.md).

## Arquitectura

```
                 ┌────────────────────────────────────────────────────┐
                 │                    main.py (CLI)                   │
                 └───────────────────────┬────────────────────────────┘
                                         ▼
                 ┌────────────────────────────────────────────────────┐
                 │              pipeline.ScrapePipeline               │
                 └───────────────────────┬────────────────────────────┘
   ┌────────────┬───────────────┬──────┴───────┬───────────────┬──────────────┐
   ▼            ▼               ▼              ▼               ▼              ▼
 discovery/  compliance/    fetchers/      analysis/      extractors/     storage/
 crawler.py  policy.py      fetchers.py    page_map.py    html / pdf      store.py
 sections.py                                coverage.py    docx            ledger.py
 ─ sitemaps  ─ robots.txt   ─ Static       ─ secciones y  ─ trafilatura   ─ <id>.md
 ─ BFS       ─ crawl-delay    Fetcher        concentra-   ─ fallbacks     ─ <id>.json
 ─ Frontier  ─ presupuesto  ─ detector       ción de        semánticos    ─ <id>.pdf
   priori-   ─ captcha/403    SPA v2         texto        ─ pymupdf4llm   ─ index.json
   zada        -> log&skip  ─ Dynamic      ─ listado vs   ─ mammoth       ─ ocr_queue
 ─ trampas                    Fetcher        artículo     ─ triaje OCR    ─ <id>.pagemap
   de query                   (Playwright) ─ paginación                     .json
                            ─ fallback       (21 métodos)                 ─ visits.jsonl
                              si el         ─ fulltext links                (libro de
                              navegador     ─ cobertura vs                   visitas)
                              falla           inventario
```

`parallel.py` orquesta un `ScrapePipeline` por dominio sobre componentes
compartidos (store, throttle, robots) y `planner.py` + `llm.py` deciden la
estrategia por sitio.

### Decisiones de diseño clave

1. **Escalera de adaptación estático → dinámico.** Toda URL se intenta primero con `httpx` (barato). Una heurística (`needs_dynamic_rendering`) decide si el HTML estático es un "cascarón" de SPA con tres señales independientes: (a) mensajes explícitos "requires JavaScript" (es/en/pt, en `<noscript>` o visibles), (b) raíz de SPA presente pero casi vacía (`#app`, `#root`, `#__next`… — cubre sitios cuyo menú server-rendered supera el umbral de texto, como Europe PMC), y (c) poco texto visible + marcadores de framework. Solo entonces se escala a Playwright, que renderiza JS, hace scroll para lazy-loading y devuelve el DOM final. El resto del pipeline es agnóstico al método.

1bis. **Mapa semántico de página (`analysis/page_map.py`).** Antes de extraer, cada HTML se analiza con heurísticas independientes del sitio: landmarks y **concentración de texto** (¿en qué contenedor vive la mayoría del contenido?), **grupos de hermanos repetidos** (misma etiqueta+clases, puntuados por tamaño, homogeneidad y densidad de enlaces, con penalización a menús) y densidad de enlaces. Con eso se clasifica la página como `listing` (serie de ítems con enlace a detalle — se encolan los detalles con prioridad y se sigue la paginación, incluida la paginación JS vía convención `?page=N+1` auto-validada por ítems nuevos), `article` (se extrae el documento y se siguen SOLO sus enlaces de texto completo: sección bajo un heading tipo "Full text links"/"Texto completo", con PDFs primero) o `navigation` (BFS clásico). El mapa completo se guarda en `analysis/<doc_id>.pagemap.json` para auditoría. El grupo repetido debe dominar el texto de la página (≥40 %): así un listado de "artículos similares" o de referencias embebido en una página de artículo no la reclasifica como listado.

1ter. **Sonda de acciones PDF (`pdf_click_probe`).** Algunos sitios (p. ej. Europe PMC) exponen el PDF como un botón JS sin `href`. Si se activa en config, el DynamicFetcher localiza textos tipo "Open PDF"/"Descargar PDF" (máx. 2, y nunca si hay muchos — sería un listado), hace clic y captura la descarga en el momento (las URLs suelen ir firmadas y expiran). Los bytes entran por el mismo camino `pdf_extractor` (pymupdf4llm + triaje OCR), con `fetch_method: playwright_click` y `discovered_on` para trazabilidad.

1quater. **Modo secciones (`discovery.mode: sections`).** El escalón "cognitivo" sobre una portada: se analiza el home, se descubren sus secciones de contenido desde la navegación (`discovery/sections.py` — etiquetas cortas en nav/header, mismo sitio, rutas de 1-3 segmentos, filtrando navegación utilitaria en es/en/pt y relegando secciones de solo-media como videos/fotos/podcasts), y cada sección se crawlea como sub-árbol con presupuesto propio (`max_pages_per_section`), de modo que una sección enorme no ahogue a las demás. El mapa de secciones con documentos recolectados por sección queda en `analysis/<seed>-sections.pagemap.json`. Perillas: `max_sections`, `max_pages_per_section`. Con `max_section_depth: 2` el árbol se vuelve **recursivo**: en la nav de cada sección se buscan sub-secciones (rutas que extienden la del padre, p.ej. `/noticias/regiones` → `/noticias/regiones/region-de-arica`) y cada nivel recibe la mitad del presupuesto (`max_subsections` por padre). El reporte por sección usa nombres jerárquicos ("Regiones > Arica").

1quinquies. **Modo agéntico (`discovery.mode: auto`) y terminación por saturación.** Con `mode: auto` el operador ya no elige estrategia: el pipeline sondea la semilla una vez y decide solo — listado → crawl clásico alimentado por ítems y paginación; artículo → ingesta dirigida (doc + adjuntos); portada con ≥3 secciones → árbol de secciones; portada opaca con sitemap poblado → vía sitemap; resto → BFS acotado. El plan y sus razones quedan en `analysis/<seed>-plan.pagemap.json`. Complementos: `exhaustive: true` levanta los topes arbitrarios de profundidad y paginación, y la **saturación** (`saturation_window`, default 25) los reemplaza como riel: si en las últimas N páginas no salió ni un documento nuevo, el sitio se cierra solo con la razón registrada en el grafo de procedencia (`terminated`). Esto también corrige el hallazgo del "rezagado": un dominio que dejó de rendir ya no puede monopolizar un worker.

1sexies. **Árbitro LLM opcional (`discovery.llm`, Qwen local).** El planner heurístico marca su decisión como dudosa cuando las señales son ambiguas (portada sin listado ni artículo claros, con pocas secciones y sin sitemap). SOLO en esos casos, y solo si hay un servidor OpenAI-compatible disponible (p.ej. Qwen2.5-32B vía vLLM), se consulta al modelo para desempatar: recibe un resumen estructural de la página (título, encabezados, muestra de textos de enlace, conteos) y devuelve la estrategia. Con una sola GPU esto mantiene el costo acotado — el modelo se invoca en el puñado de portadas dudosas, no en cada página — y el pipeline **nunca depende del LLM**: si el servidor no está, tarda o responde algo inválido, la heurística es el piso. Config: `llm: {enabled, endpoint, model, timeout}`. La decisión (`decided_by: heuristic|llm`) queda en el plan.

2. **Extracción en cascada.** `trafilatura` (estado del arte en extracción de contenido principal, salida directa en Markdown) → fallback por selectores semánticos (`<article>`, `<main>`, `.entry-content`…) con `markdownify` → fallback bruto. Se descartan páginas sin contenido sustantivo (umbral de palabras configurable).

3. **Metadata multi-fuente.** JSON-LD (schema.org), Open Graph, Dublin Core y meta tags académicos `citation_*` (revistas/repositorios científicos). Con esas señales también se clasifica la fuente: `news | scientific | blog | government | generic`.

4. **PDFs con triaje de OCR diferido.** Cada PDF descargado se analiza con PyMuPDF: caracteres por página, cobertura de imagen y páginas sin texto. Si tiene capa de texto sana, `pymupdf4llm` lo convierte a Markdown estructurado. Si parece escaneado, **no se hace OCR en el crawl**: se guarda el binario, se marca `ocr_status: pending` y se encola en `ocr_queue.json` con las señales y estrategias sugeridas. Después, `process_ocr_queue.py` aplica la estrategia elegida (ocrmypdf, tesseract, o la que implementes: VLM, servicio gestionado).

5. **Cumplimiento por diseño.** `robots.txt` se respeta siempre (Disallow y Crawl-delay), hay rate limiting y presupuesto por dominio, y el User-Agent se identifica honestamente. Los captchas y bloqueos (403/429/503, Cloudflare/DataDome/hCaptcha/reCAPTCHA…) se **detectan, se registran y se omite el dominio**: este framework no incluye ni incluirá técnicas de evasión — un captcha es el sitio diciendo explícitamente "no a bots", y saltarlo acarrea riesgos legales y de bloqueo permanente. Para fuentes protegidas, la vía correcta es su API oficial, datasets publicados o pedir permiso.

6. **Paginación exhaustiva y validada.** Un listado sin su paginación es un listado a medias, y cada gestor de contenidos la expresa distinto. Se reconocen 21 convenciones, agrupadas en cuatro familias: **parámetro** (17 nombres: `page`, `paged` de WordPress clásico, `pg`, `pageNumber` de .NET, `start`/`offset` de Joomla y DSpace…), **texto del enlace** ("Siguiente", "Página siguiente", "Ver más", `»`, `›`, `→`), **ruta** (`/page/N/`, `/pagina/N`) y **enlaces numerados** (`1 2 3 4`), esta última agnóstica a la convención de URL: cubre numeración en la ruta y parámetros que nadie ha visto antes, con la guarda de que un archivo por años ("2024 2023 2022") no se confunda con un paginador. Cuando no hay ningún enlace pero sí un contenedor de paginador sin `href` (scroll infinito, botón "cargar más"), se **sintetiza** la siguiente página incrementando el parámetro correcto — +1 para índices de página, el paso observado para parámetros de salto (`?start=20` → `?start=40`) — y se valida contra "¿aportó ítems nuevos?": si no, no se sintetiza la siguiente. Dos guardas nacieron de falsos positivos reales: un "Ver más" que se repite más de tres veces en la página es el control de cada ítem, no la página siguiente; y un `rel=next` que apunta a otro host es una fuga de configuración (un sitio anunciaba su servidor de pruebas), no la página 2. La paginación se calcula en **toda** página no vacía, no solo en las clasificadas como `listing`: las portadas tipo revista y los repositorios extraen sus ítems por la vía del "grupo fuerte" y quedan como `article`/`navigation`, así que mirar solo los `listing` las dejaba en una sola página.

7. **Libro de visitas y medición de cobertura.** El corpus guarda lo que se extrajo y el pagemap lo que se analizó; ninguno dice qué se **descartó** ni por qué, y sin eso la cobertura no se puede medir. Cada crawl escribe `_ledger/visits.jsonl`: una línea por decisión sobre cada URL vista, con vocabulario cerrado (`enqueued`, `rejected` con motivo, `saved`, `duplicate`, `listing`, `hub`, `no_document`, `blocked`, `robots`, `http_error`, `throttle`, `already_saved`, `too_large`, `empty`, `offsite_html`, `extract_error`, `not_fetched` con la razón del cierre). `analysis/coverage.py` lo cruza con un inventario construido **fuera** del pipeline (sitemap, un rastreo `wget --spider`, o una lista de la API de la institución) y `scripts/benchmark_coverage.py` reporta alcance / descarga / recall / precisión sobre las URLs de contenido, más **la lista de faltantes etiquetada por la etapa en que se perdieron**: nunca vista (hueco de navegación), sin descargar (presupuesto), o descartada (decisión de extracción, con su motivo). Esa lista es la salida útil: dice exactamente qué patrón de navegación no se reconoce. `scripts/probe_pagination.py` hace lo mismo para un listado concreto: recorre su paginación como lo haría el pipeline y verifica que cada página aporte ítems nuevos — detectar un enlace de "siguiente" no prueba nada si al seguirlo vuelve la misma página.

## Layout de salida (relación por `doc_id`)

```
data/
├── metadata/
│   ├── index.json          # índice maestro: doc_id -> {url, title, files...}
│   └── <doc_id>.json       # metadata completa (título, autores, fecha,
│                           #   idioma, categoría, señales OCR, rutas...)
├── markdown/<doc_id>.md    # texto en Markdown con front-matter
├── pdfs/<doc_id>.pdf       # binario original
├── analysis/<doc_id>.pagemap.json      # qué decidió el sistema sobre la página
├── ocr_pending/ocr_queue.json
├── _ledger/visits.jsonl    # libro de visitas: TODA URL vista y su decisión
└── _progress/completed_domains.json    # para --resume
```

`doc_id` es un hash SHA-256 (16 hex) de la URL canónica: determinístico, así las re-ejecuciones deduplican solas y cualquier `.md` se relaciona con su `.json` por nombre de archivo.

## Instalación

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # solo necesario para sitios dinámicos

# Para la etapa opcional de OCR:
pip install ocrmypdf pytesseract pillow
sudo apt install tesseract-ocr tesseract-ocr-spa   # o equivalente en tu SO
```

## Uso

```bash
# 1. Edita config.yaml: identity.user_agent (¡pon tu contacto real!) y seeds
python main.py                         # crawlea las semillas del config
python main.py --url https://sitio.com/articulo -v   # URLs puntuales
python main.py --url-file enlaces.csv               # semillas desde CSV/txt
#   CSV estilo search_links (Consulta_Origen,Titulo,URL): cada documento
#   recolectado guarda en extra.seed_context la consulta que lo originó.
#   Para ingesta dirigida usa max_depth: 1 (enlace -> doc + sus adjuntos).

# 2. Revisar PDFs escaneados detectados
python main.py --ocr-report
python main.py --report                # corpus_report.json: agregados + docs

# 3. Cuando elijas estrategia, procesar la cola
python process_ocr_queue.py --dry-run
python process_ocr_queue.py --strategy ocrmypdf --lang spa+eng
```

## Extensión

- **Nuevo tipo de fuente:** añade heurísticas en `html_extractor.classify`.
- **Extractores por sitio:** puedes registrar extractores específicos por dominio antes del genérico (patrón strategy sobre `extract_html_document`).
- **Nueva estrategia OCR:** agrega una función `run(pdf_path, lang) -> str` en `process_ocr_queue.STRATEGIES`.
- **Escala:** `parallel.py` ya paraleliza **por dominio** (`--workers N`): un `ScrapePipeline` por worker sobre un store, throttle y robots compartidos con lock interno, de modo que cada sitio sigue viendo el mismo ritmo que en modo secuencial pero el reloj de pared se divide. Es por dominio y no por URL a propósito: la cortesía se mide por sitio. Con `--resume` se saltan los dominios ya completados. Para escalar más allá de una máquina, la vía es migrar la Frontier a una cola externa (Redis) manteniendo el resto igual.
- **Cluster:** todo se lanza por Slurm (`slurm/crawl.sbatch`, `slurm/crawl_llm.sbatch`). Ojo con el `TMPDIR`: Playwright falla si `/tmp` no es escribible en el nodo — ver [`slurm/README_TMPDIR.md`](slurm/README_TMPDIR.md).

## Auditoría y trazabilidad

Cada corrida deja el material para auditar **qué se recolectó y por dónde**:

- **`crawl_path` + `depth` en la metadata de cada documento**: la cadena
  completa `seed → sección → listado → detalle → binario` (kinds: seed,
  section, sitemap, listing_item, pagination, fulltext, document_link,
  content_link, bfs, pdf_click). El grafo completo queda en
  `analysis/<seed>-provenance.pagemap.json`.
- **`python scripts/site_tree.py <data_dir>`**: reconstruye el árbol del
  sitio con documentos y palabras por rama — responde "¿qué sección produjo
  qué, a qué profundidad, y dónde se cortó el presupuesto?".
- **`python scripts/audit_corpus.py <data_dir>`**: revisión automática de
  calidad — casi-duplicados (shingles/Jaccard), boilerplate residual por
  dominio, cobertura de extracción vs pagemap, idioma vs dominio, títulos
  genéricos/duplicados y markdown dominado por enlaces. Salida priorizada
  en `audit_report.json`.
- **`python scripts/run_stats.py <log>`**: métricas de proceso (throughput,
  escaladas a render, vacías, robots-skips, OCR).
- **`python scripts/benchmark_coverage.py <data_dir> --site <url> --wget`**:
  cobertura contra un inventario independiente del sitio — alcance, descarga,
  recall, precisión, y la lista de URLs faltantes etiquetadas por la etapa en
  que se perdieron. Requiere el libro de visitas (`_ledger/visits.jsonl`), que
  los corpus anteriores a esta capacidad no tienen: hay que re-crawlear para
  medirlos.
- **`python scripts/probe_pagination.py <url> --pages 6`**: recorre la
  paginación de un listado real y verifica que cada página aporte ítems
  nuevos. Útil para decidir si un sitio necesita navegador o si su paginación
  es de las que no se pueden sintetizar (cursor opaco).

## Consideraciones legales

Respetar `robots.txt` y los términos de servicio de cada sitio es responsabilidad de quien opera el scraper. Este proyecto está pensado para recopilación de contenido público con fines legítimos (investigación, archivo, análisis), a un ritmo que no perjudique a los servidores de origen.
