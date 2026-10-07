#!/bin/bash
# WebHarvest — corrida v3 sobre el Excel ordenado de fuentes autorizadas.
#
# Uso:  sbatch slurm/corrida_v3.sbatch <config.yaml> <seeds.csv> [workers]
# El corpus se escribe donde diga storage.base_dir del config, que para v3
# apunta al espacio del proyecto en ranokau por su ruta NFS
# (/workspace1-ranokau/projects/datatrainlatamgpt/gpt/scraping/...): el job
# corre en lascar, donde /workspace1 es OTRO disco.
#
#SBATCH --job-name=wh-v3
#SBATCH --partition=lascar
#SBATCH --account=latamgpt
#SBATCH --qos=lascar-unlimit
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=48:00:00
#SBATCH --nice=100
#SBATCH --requeue
#SBATCH --output=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest/slurm/logs/v3_%x_%j.log
#SBATCH --chdir=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest
set -u
PROJ=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest
CONFIG="${1:?falta config.yaml}"
SEEDS="${2:?falta seeds.csv}"
WORKERS="${3:-12}"
# TMPDIR propio: red de seguridad para Playwright (ver slurm/README_TMPDIR.md)
export TMPDIR="$PROJ/slurm/tmp/$SLURM_JOB_ID"; mkdir -p "$TMPDIR"
trap 'rm -rf "$TMPDIR" 2>/dev/null || true' EXIT
cd "$PROJ"
echo "== $(hostname) | job $SLURM_JOB_ID | $CONFIG | $SEEDS | workers=$WORKERS | $(date -Is)"
# --resume: si el job se re-encola o se relanza, salta los dominios ya completos
exec "$PROJ/.venv/bin/python" -u main.py --config "$CONFIG" --url-file "$SEEDS" \
     --workers "$WORKERS" --resume
