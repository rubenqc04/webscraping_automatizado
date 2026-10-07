# Guía básica de Slurm para correr WebHarvest

Guía de operación paso a paso. Los detalles del cluster (particiones, cuentas,
discos) están en `../SLURM.md`; esto es el "cómo se maneja en el día a día".

---

## 1. La idea en una frase

En este cluster **no se ejecutan procesos a mano**. Se le *entrega un script* a
Slurm, que lo pone en una cola y lo corre en un nodo cuando hay recursos. Tú
recuperas la sesión de inmediato — el trabajo sigue solo, aunque cierres todo.

```
    tú  ──sbatch──▶  COLA de Slurm  ──cuando hay cupo──▶  corre en lascar/ranokau
                                                            └─▶ escribe a un .log
```

---

## 2. Los 6 comandos que necesitas

| Comando | Para qué |
|---|---|
| `sbatch script.sbatch` | **enviar** un trabajo a la cola |
| `squeue -u $USER` | **ver** tus trabajos (en cola / corriendo) |
| `scancel <JOBID>` | **cancelar** un trabajo |
| `sinfo` | ver nodos y si hay capacidad libre |
| `tail -f <archivo.log>` | **seguir en vivo** lo que imprime el trabajo |
| `sacct -j <JOBID>` | ver cómo terminó (estado, tiempo, memoria) |

---

## 3. Enviar un crawl — el flujo completo

```bash
cd /workspace1/users/rubenqc/gpt/scrap/automatizacion/webharvest

# 1) enviar (crawl normal, sin LLM). Devuelve un número de job al instante.
sbatch slurm/crawl.sbatch configs/prod_autorizadas_homes_v2.yaml \
                          configs/seeds/seeds_autorizadas_homes.csv
#   -> "Submitted batch job 712"

# 2) ¿está corriendo ya o esperando cupo?
squeue -u $USER
#   ST=R corriendo · ST=PD en cola. La columna NODELIST(REASON) dice por qué espera.

# 3) seguir el avance en vivo (Ctrl-C solo corta el 'tail', NO el trabajo)
tail -f slurm/logs/crawl_712.log

# 4) cuando termine, ver cómo salió
sacct -j 712 --format=JobID,State,Elapsed,MaxRSS,ExitCode
```

Con **árbitro LLM** (Qwen 32B), el único cambio es el script — pide 1 GPU y
levanta el modelo solo:

```bash
sbatch slurm/crawl_llm.sbatch configs/battery_auto_llm.yaml \
                              configs/seeds/seeds_autorizadas_homes.csv
tail -f slurm/logs/crawl_llm_<JOBID>.log     # el crawl
tail -f slurm/logs/vllm_<JOBID>.log          # el servidor Qwen
```

---

## 4. Leer `squeue` (lo que más vas a mirar)

```
 JOBID PARTITION     NAME     USER ST   TIME  NODES NODELIST(REASON)
   712    lascar wh-crawl  rubenqc  R   3:20      1 lascar
   713    lascar wh-craw+  rubenqc PD   0:00      1 (Priority)
```

- **ST = R**: corriendo. **PD**: pendiente (en cola).
- **(REASON)** en PD: `Priority`/`Resources` = esperando cupo (normal);
  `QOSMaxWallDurationPerJobLimit` = pediste más tiempo del permitido, etc.

---

## 5. Cosas que evitan problemas

- **`--resume` ya está en los scripts.** Si un trabajo se corta (se acaba el
  tiempo, cae el nodo, lo cancelas), vuelve a enviar el MISMO `sbatch`: retoma
  donde quedó, no reempieza. Los dominios ya hechos se saltan solos.
- **Cancelar sin miedo.** `scancel <JOBID>` es seguro: lo ya recolectado queda
  en disco. Para cancelar todos tus jobs de una: `scancel -u $USER`.
- **No cambies `/workspace1` por otra ruta en los scripts.** Usan
  `/workspace1-ranokau/...` a propósito (es la única ruta que el proyecto tiene
  desde cualquier nodo — ver `../SLURM.md`).
- **Sé buen vecino.** Los scripts traen `--nice=100`: tus jobs llenan capacidad
  ociosa sin quitarle el turno al resto del equipo. No lo subas a 0.
- **Una prueba corta antes de una corrida larga.** Crea un CSV con 3–4 URLs y
  envíalo primero; confirmas que todo arranca en ~2 min antes de lanzar cientos.

---

## 6. Si algo sale mal

| Síntoma | Qué mirar |
|---|---|
| El job pasa a `PD` y no arranca | `squeue` → columna REASON. Casi siempre es esperar cupo. |
| Termina enseguida con error | `sacct -j <ID> --format=JobID,State,ExitCode` y luego el `.log`. |
| `sbatch: error: ... Invalid qos` | La cuenta/QOS no aplica a esa partición (ver `../SLURM.md`). |
| No aparece el `.log` | Se crea al arrancar, no al enviar. Si sigue en `PD`, aún no existe. |

**Ensayo sin gastar recursos** (Slurm dice dónde y cuándo correría, sin lanzar):

```bash
sbatch --test-only slurm/crawl.sbatch <config> <seeds>
```

---

## 7. Ver una corrida en vivo (panel web)

`scripts/dashboard.py` lee el libro de visitas (`_ledger/visits.jsonl`) y los
metadatos de cada corpus mientras el trabajo escribe, y los sirve como una
página que se actualiza cada 3 s: documentos y palabras, ritmo por minuto,
estado de cada dominio (activo / en pausa / terminado / pendiente), la URL que
se está procesando, últimos eventos y fallas con su motivo. Solo lee archivos:
no toca la corrida.

```bash
B=/workspace1/projects/datatrainlatamgpt/gpt/scraping/autorizadas_v3
python scripts/dashboard.py $B/portadas $B/profundas \
    --seeds configs/seeds/v3/portadas.csv configs/seeds/v3/profundas.csv --port 8765
```

Abrirlo desde tu computador: en VS Code, pestaña **PORTS** → reenviar 8765;
o por SSH `ssh -L 8765:localhost:8765 rubenqc@ranokau` y luego
`http://localhost:8765`. Con `--seeds`, los dominios que aún no empiezan
aparecen como *pendiente*. "Cupo agotado" no cuenta como falla: es el tope de
páginas por dominio que fija la configuración.

**Velocidad en tokens.** Los tokens se estiman como *caracteres ÷ 4* (la
convención del equipo) sobre el texto guardado en `markdown/`. El panel muestra
tok/s de los últimos 10 min, tok/s promedio, la proyección a 24 h y tok/s por
dominio (solo cuando el dominio lleva ≥ 10 min produciendo). Documentos,
palabras y tokens se cuentan desde `metadata/`, no desde las entradas "saved"
del libro de visitas, que sobrecuenta: una misma noticia publicada en varios
sitios hermanos se guarda una sola vez.

### El panel para todo el equipo (Space privado de Hugging Face)

https://huggingface.co/spaces/latam-gpt/webharvest-dashboard — privado: lo ven
solo los miembros de la organización `latam-gpt` con su sesión de HF iniciada.

Un Space no ve los archivos del cluster, así que el cluster le *publica* el
estado: `slurm/dashboard_push.sbatch` calcula el estado cada 2 min y lo sube
como `state.json` (un commit; cada 50 se compacta el historial). La página se
relee sola cada 20 s. Es un Space **estático** porque la organización no
tiene plan pagado (los Spaces Docker/Gradio lo requieren); por eso el panel
del Space va ~2 min atrás, y el local, segundos.

```bash
sbatch slurm/dashboard_push.sbatch        # empezar a publicar (corre en lascar, 7 días)
scancel --name=wh-panel                   # dejar de publicar
python scripts/deploy_space.py            # crear el Space o actualizar la página
tail -f slurm/logs/panel_<JOBID>.log      # un "ok · 114 KB" por envío
```

Para otra corrida, cambia las rutas `B=` y `--seeds` dentro del sbatch.
