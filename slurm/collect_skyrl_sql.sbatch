#!/bin/bash
# Collect SkyRL-SQL rollouts on one GPU node (one vLLM shard per A100), merge
# them into one harness trace, and replay it under the cache modes.
# Run slurm/setup_skyrl_sql.sh on a login node first. Submit with e.g.
#   sbatch -A <account>_g -q regular slurm/collect_skyrl_sql.sbatch
# Optional environment: PASSES (default 3), SAMPLES (5), LIMIT (questions per
# shard; unset = all 653), SKYRL_SQL_DIR, SKYRL_SQL_MODEL.
#SBATCH -J fuzzytv-skyrl-sql
#SBATCH -C gpu
#SBATCH -N 1
#SBATCH --gpus-per-node=4
#SBATCH -c 64
#SBATCH -t 02:00:00
#SBATCH -o /dev/null
set -euo pipefail

REPO="${SLURM_SUBMIT_DIR:-$(pwd)}"
DIR="${SKYRL_SQL_DIR:-$SCRATCH/skyrl-sql}"
MODEL="${SKYRL_SQL_MODEL:-Qwen/Qwen2.5-Coder-7B-Instruct}"
JOB="${SLURM_JOB_ID:-local}"
OUT="${FUZZYTV_OUT:-$SCRATCH/fuzzy-tvcache}/skyrl-sql/$JOB"
mkdir -p "$OUT"
exec > >(tee "$OUT/job.log") 2>&1

cd "$REPO"
export HF_HOME="$DIR/hf" HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
DATA="$(find "$DIR/data" -name train.parquet -print -quit)"
DB_ROOT="$(dirname "$(find "$DIR/db" -maxdepth 3 -type d -name SynSQL-2.5M -print -quit)")"
[[ -f "$DATA" && -d "$DB_ROOT" ]] || { echo "run slurm/setup_skyrl_sql.sh first"; exit 1; }
nvidia-smi --query-gpu=name,memory.total --format=csv

SHARDS=4
pids=()
for i in $(seq 0 $((SHARDS - 1))); do
    CUDA_VISIBLE_DEVICES=$i .venv-collect/bin/python -m fuzzytv.eval.skyrl_sql \
        --data "$DATA" --db-root "$DB_ROOT" --model "$MODEL" \
        --samples "${SAMPLES:-5}" --passes "${PASSES:-3}" --shard "$i/$SHARDS" \
        ${LIMIT:+--limit "$LIMIT"} --seed 0 --out "$OUT/shard$i.json" \
        > "$OUT/shard$i.log" 2>&1 &
    pids+=($!)
done
status=0
for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
        echo "shard $i failed; last lines:"; tail -30 "$OUT/shard$i.log"; status=1
    fi
done
[[ $status == 0 ]] || exit 1
grep -h "SQL calls" "$OUT"/shard*.log

TRACE="$OUT/skyrl-sql.json"
.venv-collect/bin/python -m fuzzytv.eval merge "$OUT"/shard?.json -o "$TRACE" --name skyrl-sql
cat "$OUT"/shard?.json.rollouts.jsonl > "$OUT/rollouts.jsonl"
.venv-collect/bin/python - "$OUT/rollouts.jsonl" <<'PY'
import collections, json, sys
rows = [json.loads(line) for line in open(sys.argv[1])]
print(f"rollouts={len(rows)} mean_reward={sum(r['reward'] for r in rows) / len(rows):.3f}",
      dict(collections.Counter(r["stop_reason"] for r in rows)),
      f"mean_calls={sum(len(r['calls']) for r in rows) / len(rows):.2f}")
PY

# Replay (CPU only). `:real` modes use fuzzytv.sql if present; the runner skips
# them otherwise. Every rollout is validated against direct execution.
export FUZZYTV_SKYRL_SQL_DB_ROOT="$DB_ROOT"
.venv-collect/bin/pip install -q -e '.[eval]'
.venv-collect/bin/python -m fuzzytv.eval run "$TRACE" \
    --modes none exact canonical:real relevant_state:real \
    --read-only-hints off --latency-scale 0 --out "$OUT" --name replay --plot
echo "trace: $TRACE"
