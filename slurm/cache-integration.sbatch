#!/bin/bash
# Submit with allocation/QOS/constraint options on the command line.
#SBATCH -J fuzzytv-cache-integration
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 2
#SBATCH -t 00:15:00
#SBATCH -o /dev/null
set -euo pipefail

REPO="${SLURM_SUBMIT_DIR:-$(pwd)}"
OUT="${FUZZYTV_OUT:-$SCRATCH/fuzzy-tvcache}/integration/${SLURM_JOB_ID:-local}"
mkdir -p "$OUT"
exec > >(tee "$OUT/job.log") 2>&1

module load python/3.13-26.8.1 rust/stable
cd "$REPO"
source .venv/bin/activate

# Build from a scratch copy: compute nodes cannot lock Cargo.lock on the
# repository's home filesystem. No upstream sources or pointers are changed.
mkdir -p "$OUT/server-source/src"
cp upstream/tvcache-rust/Cargo.toml upstream/tvcache-rust/Cargo.lock "$OUT/server-source/"
cp -R upstream/tvcache-rust/src/. "$OUT/server-source/src/"
# Cargo also locks its package cache, so stage cached dependencies on scratch.
# Copy the registry only; do not copy credentials or user configuration.
cargo_cache_source="${CARGO_HOME:-$HOME/.cargo}"
export CARGO_HOME="$OUT/cargo-home"
mkdir -p "$CARGO_HOME/registry"
cp -R "$cargo_cache_source/registry/." "$CARGO_HOME/registry/"
cargo build --offline --locked --manifest-path "$OUT/server-source/Cargo.toml" \
    --target-dir "$OUT/cargo-target"
pytest -q

# The pinned server binds port 7878. Do not connect to an existing server.
python - <<'PY'
import socket

with socket.socket() as listener:
    listener.bind(("0.0.0.0", 7878))
PY

TOKIO_WORKER_THREADS=2 "$OUT/cargo-target/debug/tvcache" \
    --eviction-policy=none > "$OUT/server.log" 2>&1 &
cache_server_pid=$!

cleanup() {
    kill "$cache_server_pid" 2>/dev/null || true
    wait "$cache_server_pid" 2>/dev/null || true
}
trap cleanup EXIT

cache_server_ready=0
for attempt in {1..100}; do
    if ! kill -0 "$cache_server_pid" 2>/dev/null; then
        cat "$OUT/server.log"
        exit 1
    fi
    if python - <<'PY'
import socket
import sys

try:
    with socket.create_connection(("127.0.0.1", 7878), timeout=0.2):
        pass
except OSError:
    sys.exit(1)
PY
    then
        cache_server_ready=1
        break
    fi
    sleep 0.1
done

if [[ "$cache_server_ready" != 1 ]]; then
    cat "$OUT/server.log"
    exit 1
fi

export TVCACHE_HOST=127.0.0.1
export TVCACHE_PORT=7878
pytest -q -s --run-tvcache-integration tests/integration
