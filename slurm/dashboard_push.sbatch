#!/bin/bash
# WebHarvest — envía el estado de una corrida al panel del Space privado.
#
# Uso:  sbatch slurm/dashboard_push.sbatch
#       (la corrida y el Space se configuran abajo; ver scripts/deploy_space.py)
# Proceso liviano: cada 2 min calcula el estado y lo sube como state.json al
# Space estático privado (un commit; cada 50 se compacta el historial). No toca
# la corrida. Corre en lascar, por eso las rutas van por /workspace1-ranokau.
# Detenerlo:  scancel --name=wh-panel
#
#SBATCH --job-name=wh-panel
#SBATCH --partition=lascar
#SBATCH --account=latamgpt
#SBATCH --qos=lascar-unlimit
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=7-00:00:00
#SBATCH --nice=100
#SBATCH --output=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest/slurm/logs/panel_%j.log
#SBATCH --chdir=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest
set -u
PROJ=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest
B=/workspace1-ranokau/projects/datatrainlatamgpt/gpt/scraping/autorizadas_v3
SPACE="${SPACE:-latam-gpt/webharvest-dashboard}"
export WEBHARVEST_HF_TOKEN_FILE="${WEBHARVEST_HF_TOKEN_FILE:-/workspace1-ranokau/users/rubenqc/.cache/huggingface/token}"
cd "$PROJ"
echo "== $(hostname) | job $SLURM_JOB_ID | -> $SPACE | $(date -Is)"
exec "$PROJ/.venv/bin/python" -u scripts/dashboard.py "$B/portadas" "$B/profundas" \
     --seeds configs/seeds/v3/portadas.csv configs/seeds/v3/profundas.csv \
     --push "$SPACE" --every 120
