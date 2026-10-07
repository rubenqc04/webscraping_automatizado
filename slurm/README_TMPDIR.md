# El navegador y `/tmp` en los nodos de cómputo

Playwright crea su directorio de trabajo con `mkdtemp` **en `/tmp`**, y en
los nodos de cómputo `/tmp` tiene la cuota agotada. El síntoma:

```
playwright._impl._errors.Error: BrowserType.launch: Unknown system error -122,
mkdtemp '/tmp/playwright-artifacts-XXXXXX'
```

Verificado en lascar (job 4867): `touch /tmp/archivo` funciona, pero crear un
directorio temporal falla. Consecuencia: **el navegador no arrancaba en ningún
crawl lanzado por Slurm**, así que todas las páginas que necesitan JavaScript
se saltaban sin que se notara (una corrida de 4 sitios dio 49 documentos,
todos por `httpx`, ninguno por `playwright`).

Solución: dar al job su propio `TMPDIR` antes de ejecutar el crawl.

```bash
export TMPDIR="$PROJ/slurm/tmp/$SLURM_JOB_ID"
mkdir -p "$TMPDIR"
trap 'rm -rf "$TMPDIR" 2>/dev/null || true' EXIT
```

Ya está en `crawl_llm.sbatch`. **`crawl.sbatch` lo necesita también**: no se
tocó porque tiene ediciones locales sin resolver (tres líneas al inicio que
activan un venv inexistente y llaman a `main.py` sin argumentos).

Al limpiar el `TMPDIR` sobre NFS quedan archivos `.nfs*` en uso; de ahí el
`|| true` del `trap`: no es un error que deba hacer fallar el job.

Como red de seguridad, desde el arreglo del fetcher un fallo del navegador
degrada esa página al HTML estático en vez de abortar el sitio completo.
