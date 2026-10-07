# Ejecutar WebHarvest en el cluster Slurm

Exploración del cluster medida el **2026-09-02**. Todos los valores salen de
`sinfo`/`scontrol`/`sacctmgr` de este cluster, no de supuestos.

## El cluster

| | |
|---|---|
| Slurm | 26.05.0 |
| Nodos | **lascar** y **ranokau** — 2× A100-SXM4-80GB × 8 GPU cada uno |
| CPUs/nodo | 240 asignables (256 físicas; `CR_CORE_MEMORY` cobra 2 CPU por core) |
| RAM | lascar ~1 TB · ranokau ~2 TB |
| Particiones | `cenia*` (ambos nodos) · `lascar` · `ranokau` |

### Cuentas y QOS (lo que puedes usar)

| Cuenta | QOS | Límite de tiempo | Dónde |
|---|---|---|---|
| **`latamgpt`** | `lascar-unlimit` | 14 días | **solo** partición `lascar` |
| `default-a…` | `debug` | 1 h | `cenia` |
| `default-a…` | `regular` | 1 día, máx 4 jobs | `cenia` |

> **Regla dura descubierta:** la cuenta `latamgpt` solo tiene `lascar-unlimit`,
> que **solo corre en la partición `lascar`**. Para usar `cenia` (ambos nodos)
> hace falta QOS `regular`, que pertenece a la cuenta `default`, no a latamgpt
> — obtené su nombre completo con
> `sacctmgr -n show assoc user=$USER format=Account%30,QOS%30`.

## Topología de disco — CRÍTICA para el scraper

`/workspace1` es un disco **local del nodo donde corre el proceso** (xfs), no un
filesystem compartido. Desde cualquier nodo, el disco de cada nodo se ve por NFS:

```
/workspace1          -> disco LOCAL de este nodo
/workspace1-lascar   -> NFS al disco de lascar
/workspace1-ranokau  -> NFS al disco de ranokau
```

**El proyecto vive en el disco de `ranokau`** (`/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion`).
Consecuencia:

- Un job en `lascar` ve el proyecto **solo** como `/workspace1-ranokau/...` (NFS).
- El corpus de salida y el `_progress/` de `--resume` deben escribirse a una ruta
  **estable entre nodos** — usar siempre la ruta `-ranokau` absoluta, nunca
  `/workspace1` (que apunta a un disco distinto según el nodo).
- El `.venv` de webharvest y el modelo Qwen también están en el disco de ranokau.

## Cómo lanzar

### A) Crawl CPU-only (sin árbitro LLM) — el caso normal

El scraper es I/O-bound (espera crawl-delays de red), no CPU-bound: `--workers 8`
usa ~8 hilos. Un job de 8–16 CPU basta y sobra.

```bash
sbatch webharvest/slurm/crawl.sbatch configs/prod_autorizadas_homes_v2.yaml \
                                      configs/seeds/seeds_autorizadas_homes.csv
```

### B) Crawl + árbitro LLM (Qwen 32B)

El scraper y el servidor vLLM deben correr en el **mismo nodo** (el árbitro llama
a `localhost:8811`). Se pide 1 GPU en el mismo job: el sbatch arranca el servidor,
espera su `/health`, corre el crawl y al final apaga el servidor.

```bash
sbatch webharvest/slurm/crawl_llm.sbatch <config.yaml> <seeds.csv>
```

## Disponibilidad al momento de medir (2026-09-02)

| Nodo | GPUs libres | CPUs libres |
|---|---|---|
| ranokau | 5 de 8 | ~192 de 240 |
| lascar | 4 de 8 | ~142 de 240 |

Ambos nodos en estado `MIXED` (parcialmente ocupados, aceptan más). Tu propio
array `o2patch` ocupa parte de lascar con throttle 32.

## Reglas de convivencia (del sbatch del equipo)

- `--nice=100` (o más): tus jobs ceden prioridad y llenan capacidad ociosa en vez
  de competir — cortesía con el resto del equipo.
- `--requeue`: si un nodo cae, el job se re-encola solo; combinado con `--resume`
  del scraper, retoma sin repetir dominios.
- Nunca lanzar procesos sueltos con `nohup`/`setsid` ni tomar GPUs con
  `CUDA_VISIBLE_DEVICES` a mano: **todo pasa por Slurm**, que contabiliza y evita
  colisiones de GPU entre usuarios.
