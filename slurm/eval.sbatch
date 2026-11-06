#!/bin/bash
# Baseline + mode comparison against a real TVCache server (owner: Rohan).
# Submit with allocation/QOS/constraint options on the command line, e.g.
#   sbatch -A <account> -q <qos> -C cpu slurm/eval.sbatch
# Optional environment: EVAL_WORKLOADS (space-separated trace paths),
# EVAL_ARGS (extra `python -m fuzzytv.eval run` flags), EVAL_NAME.
#SBATCH -J fuzzytv-eval
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 4
#SBATCH -t 01:00:00
#SBATCH -o /dev/null
set -euo pipefail

REPO="${SLURM_SUBMIT_DIR:-$(pwd)}"
ROOT="${FUZZYTV_OUT:-$SCRATCH/fuzzy-tvcache}"
JOB="${SLURM_JOB_ID:-local}"
OUT="$ROOT/eval-jobs/$JOB"
mkdir -p "$OUT"
exec > >(tee "$OUT/job.log") 2>&1

module load python/3.13-26.8.1 rust/stable
cd "$REPO"
source .venv/bin/activate
pip install -q -e '.[eval]'

# Build from a scratch copy (compute nodes cannot lock Cargo files on home);
# same procedure as slurm/cache-integration.sbatch.
mkdir -p "$OUT/server-source/src"
cp upstream/tvcache-rust/Cargo.toml upstream/tvcache-rust/Cargo.lock "$OUT/server-source/"
cp -R upstream/tvcache-rust/src/. "$OUT/server-source/src/"
cargo_cache_source="${CARGO_HOME:-$HOME/.cargo}"
export CARGO_HOME="$OUT/cargo-home"
mkdir -p "$CARGO_HOME/registry"
cp -R "$cargo_cache_source/registry/." "$CARGO_HOME/registry/"
cargo build --release --offline --locked \
    --manifest-path "$OUT/server-source/Cargo.toml" --target-dir "$OUT/cargo-target"

pytest -q

# The pinned server binds port 7878. Never reuse an existing server.
python - <<'PY'
import socket

with socket.socket() as listener:
    listener.bind(("0.0.0.0", 7878))
PY

TOKIO_WORKER_THREADS=2 "$OUT/cargo-target/release/tvcache" \
    --eviction-policy=none > "$OUT/server.log" 2>&1 &
server_pid=$!
cleanup() {
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT

for attempt in {1..100}; do
    kill -0 "$server_pid" 2>/dev/null || { cat "$OUT/server.log"; exit 1; }
    python -c 'import socket; socket.create_connection(("127.0.0.1", 7878), 0.2)' \
        2>/dev/null && break
    sleep 0.1
done

export TVCACHE_HOST=127.0.0.1
export TVCACHE_PORT=7878

# 1. Memory backend must make identical decisions to this server.
pytest -q -s --run-tvcache-integration tests/integration

# 2. All modes, both hint settings, every rollout validated, real server.
WORKLOADS="${EVAL_WORKLOADS:-eval/workloads/sql-small.json eval/workloads/fs-small.json}"
# shellcheck disable=SC2086
python -m fuzzytv.eval run $WORKLOADS --backend server --sample-rate 1.0 \
    --out "$ROOT" --name "${EVAL_NAME:-server-$JOB}" --plot ${EVAL_ARGS:-}
