# Evaluación de motores OCR — 2026-08-29

Comparativa sobre el **corpus real** recolectado por WebHarvest (cola OCR de
CEPAL) más un control con ground truth. Harness reproducible: `eval_ocr.py`
(venv en `../../ocr-eval/.venv`, motores CPU + servidor VLM compartido).

## Diseño

| Clase | Documento real | Verdad |
|---|---|---|
| A: impresión limpia | CEPAL 2013 (digital) | capa de texto nativa = ground truth; se rasteriza y cada motor la reconstruye |
| B: escaneo antiguo | 364 págs, 0 chars, 100% imagen | consenso entre motores + inspección |
| C: escaneo denso | 112 págs, 87% imagen | ídem |
| D: capa corrupta | 160 págs, ToUnicode roto | ídem (OCR desde el render) |

## Resultados

**Clase A (con ground truth objetivo):**

| Motor | token-F1 | s/pág | Ejecución |
|---|---|---|---|
| **paddle_vl** (PaddleOCR-VL-1.6-0.9B, servidor vLLM :8118) | **0.986** | 3.1 | GPU compartida |
| easyocr | 0.984 | 9.6 | CPU |
| doctr | 0.890 | 2.2 | CPU |
| rapidocr (PP-OCR/ONNX) | 0.870 | 4.2 | CPU |

**Clases B/C/D (consenso):** `easyocr~paddle_vl` acuerdan 0.95–0.98 en todas
las clases (validación mutua: convergen a la misma lectura); doctr y rapidocr
divergen. Muestra del escaneo antiguo (B):

- rapidocr: `AMERICALATINAYELCARIBE:RELACIONAHORRO-INVERSION` (pierde espacios y tildes)
- doctr: `AMÉRICA LATINA Y EL CARIBE: RELACION AHORRO-INVERSION` (pierde tildes)
- easyocr: `Gráfico /.11 AMÉRICA LATINA Y EL CARIBE: RELACIÓN AHORRO-INVERSIÓN` ✓
- paddle_vl: `Gráfico I.11 AMÉRICA LATINA Y EL CARIBE: RELACIÓN AHORRO-INVERSION` ✓ (única que lee bien el numeral romano)

## Recomendación por complejidad (implementada en `process_ocr_queue.py --strategy auto`)

| Complejidad | Motor | Por qué |
|---|---|---|
| Escaneo degradado, tablas, layout complejo, capa corrupta | **paddle_vl** | mejor calidad Y más rápido (3–5 s/pág); requiere el servidor compartido |
| Impresión moderna limpia | **rapidocr** | local, liviano (ONNX sin torch), calidad suficiente |
| Sin servidor VLM disponible | **easyocr** | misma calidad que paddle_vl, autónomo, 3–4× más lento en CPU (venv `ocr-eval`) |
| Se necesita PDF “searchable” (capa dentro del PDF) | **ocrmypdf** | única opción que devuelve PDF+capa; **pendiente**: requiere tesseract del sistema (sudo) |

Reglas de `auto` (desde las señales del triaje del crawl):
`image_area_ratio > 0.5` o `bad_text_page_ratio > 0.5` o `avg_chars_per_page < 50` → paddle_vl; si no → rapidocr.

## No evaluados y por qué

- **tesseract/ocrmypdf**: sin binario del sistema ni sudo en esta máquina.
- **Surya/marker, Qwen2.5-VL propio**: requieren GPU propia; restricción del
  proyecto: GPUs 0–4 y 6–7 vedadas, GPU 5 ocupada (vLLM 72B).
- **Servicios cloud (Document AI, Textract, Azure)**: enviar el corpus a un
  tercero necesita decisión explícita de datos/costos; innecesario con
  paddle_vl local dando F1≈0.99.

Salidas: `output/results.json` (métricas por página) y `output/samples/`
(texto completo por motor y clase, para inspección).
