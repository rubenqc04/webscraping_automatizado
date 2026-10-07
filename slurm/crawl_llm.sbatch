#!/bin/bash
# WebHarvest — crawl con árbitro LLM (Qwen2.5-32B) en Slurm.
#
# El scraper y el servidor vLLM corren en el MISMO job/nodo: el árbitro llama a
# localhost:8811, así que la GPU pedida y el servidor tienen que estar juntos.
# El job: arranca vLLM en la GPU asignada, espera su /health, corre el crawl,
# y apaga el servidor al terminar (trap EXIT — también si el crawl falla).
#
# GPU: Slurm asigna una A100 vía --gres; NO se usa CUDA_VISIBLE_DEVICES a mano,
# el gres ya la aísla. 1 GPU basta: Qwen 32B bf16 (~64 GB) entra en 80 GB con
# max-model-len acotado (el planner solo necesita ~2-4k de contexto).
#
# Uso:
#   sbatch webharvest/slurm/crawl_llm.sbatch <config.yaml> <seeds.csv>
#   # el config debe tener discovery.llm.enabled: true y endpoint localhost:8811
#
#SBATCH --job-name=wh-crawl-llm
#SBATCH --partition=lascar
#SBATCH --account=latamgpt
#SBATCH --qos=lascar-unlimit
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --gres=gpu:a100:1
#SBATCH --time=24:00:00
#SBATCH --nice=100
#SBATCH --requeue
#SBATCH --output=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest/slurm/logs/crawl_llm_%j.log

set -u
PROJ=/workspace1-ranokau/users/rubenqc/gpt/scrap/automatizacion/webharvest
PY="$PROJ/.venv/bin/python"
VLLM=/home/ouhenio/benchmark_interno_manual/.venv/bin/vllm
MODEL=/workspace1-ranokau/projects/datatrainlatamgpt/models/Qwen2.5-32B-Instruct
CONFIG="${1:?falta config.yaml}"
SEEDS="${2:?falta archivo de seeds}"
WORKERS="${WORKERS:-6}"
PORT=8811

# Playwright crea su directorio de trabajo con mkdtemp en /tmp, y en los
# nodos de cómputo /tmp tiene la cuota agotada: falla con "Unknown system
# error -122" y el navegador no arranca (todas las páginas dinámicas se
# saltaban en silencio). Con TMPDIR propio sí arranca.
export TMPDIR="$PROJ/slurm/tmp/$SLURM_JOB_ID"
mkdir -p "$TMPDIR"

mkdir -p "$PROJ/slurm/logs"
cd "$PROJ"
echo "== wh-crawl-llm en $(hostname) | job $SLURM_JOB_ID | GPU $CUDA_VISIBLE_DEVICES =="

# --- servidor Qwen en la GPU que Slurm nos dio (el gres la aísla) ---
"$VLLM" serve "$MODEL" --served-model-name qwen2.5-32b-instruct \
    --port "$PORT" --dtype bfloat16 --tensor-parallel-size 1 \
    --gpu-memory-utilization 0.90 --max-model-len 8192 \
    --disable-log-requests > "$PROJ/slurm/logs/vllm_${SLURM_JOB_ID}.log" 2>&1 &
VLLM_PID=$!
# UN solo trap EXIT: bash solo guarda el último, así que apagar el servidor y
# limpiar el TMPDIR tienen que ir juntos.
cleanup() {
  echo "apagando vLLM $VLLM_PID"
  kill "$VLLM_PID" 2>/dev/null || true
  rm -rf "$TMPDIR" 2>/dev/null || true    # los .nfs* en uso no son un error
}
trap cleanup EXIT

echo "esperando /health de vLLM ..."
for i in $(seq 1 60); do
  curl -s --max-time 3 "http://localhost:$PORT/health" >/dev/null 2>&1 && { echo "vLLM listo tras ~$((i*10))s"; break; }
  kill -0 $VLLM_PID 2>/dev/null || { echo "vLLM murió al cargar; ver vllm_${SLURM_JOB_ID}.log"; exit 1; }
  sleep 10
done

# --- crawl con el árbitro apuntando al servidor local ---
"$PY" -u main.py --config "$CONFIG" --url-file "$SEEDS" \
    --workers "$WORKERS" --resume
echo "crawl terminado (rc=$?)"
