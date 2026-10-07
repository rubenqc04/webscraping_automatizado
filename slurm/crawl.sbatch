#!/bin/bash
# WebHarvest — crawl CPU-only en Slurm (sin árbitro LLM).
#
# El scraper es I/O-bound: pasa el tiempo esperando el crawl-delay de cada
# sitio, no calculando. Con --workers 8 usa ~8 hilos; 16 CPU es holgado.
#
# Rutas: TODO en el disco de ranokau, vía su ruta NFS ABSOLUTA, para que el
# job vea lo mismo corra donde corra (ver SLURM.md: /workspace1 es local por
# nodo, no compartido).
#
# Uso:
#   sbatch webharvest/slurm/crawl.sbatch <config.yaml> <seeds.csv|seeds.txt>
#   # el config y las seeds se dan RELATIVOS a webharvest/
#
#SBATCH --job-name=wh-crawl
#SBATCH --partition=lascar
#SBATCH --account=latamgpt
#SBATCH --qos=lascar-unlimit
#SBATCH --cpus-per-task=16
#SBATCH --mem=24G
#SBATCH --time=24:00:00
#SBATCH --nice=100
#SBATCH --requeue
#SBATCH --output=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest/slurm/logs/crawl_%j.log

set -u
PROJ=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest
PY="$PROJ/.venv/bin/python"
CONFIG="${1:?falta config.yaml}"
SEEDS="${2:?falta archivo de seeds}"
WORKERS="${WORKERS:-8}"

mkdir -p "$PROJ/slurm/logs"
cd "$PROJ"
echo "== wh-crawl en $(hostname) | job $SLURM_JOB_ID =="
echo "config=$CONFIG seeds=$SEEDS workers=$WORKERS"

# --resume: si el job se re-encola (--requeue) o se relanza, omite los
# dominios ya completados en _progress/ en vez de empezar de cero.
exec "$PY" -u main.py --config "$CONFIG" --url-file "$SEEDS" \
     --workers "$WORKERS" --resume
