#!/bin/bash
# Compute-node smoke test: build tvcache-rust and run the Python test suite.
# Account/QOS/etc. are passed on the command line so nothing allocation-specific is committed:
#   sbatch -A <account> -q <qos> -C <cpu|gpu> slurm/smoke.sbatch
#SBATCH -J fuzzytv-smoke
#SBATCH -N 1
#SBATCH -t 00:15:00
#SBATCH -o slurm-%j.out
set -euo pipefail

REPO="${SLURM_SUBMIT_DIR:-$(pwd)}"
OUT="${FUZZYTV_OUT:-$SCRATCH/fuzzy-tvcache}/smoke/${SLURM_JOB_ID:-local}"
mkdir -p "$OUT"

module load python/3.13-26.8.1 rust/stable
cd "$REPO"
source .venv/bin/activate

(cd upstream/tvcache-rust && cargo build --release) 2>&1 | tee "$OUT/cargo-build.log"
pytest -q 2>&1 | tee "$OUT/pytest.log"
