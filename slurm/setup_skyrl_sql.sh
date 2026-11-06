#!/bin/bash
# One-time setup for SkyRL-SQL rollout collection. Run on a Perlmutter LOGIN
# node (it needs internet): bash slurm/setup_skyrl_sql.sh
#
# Creates .venv-collect (vLLM; kept apart from .venv) and downloads, under
# $SKYRL_SQL_DIR (default $SCRATCH/skyrl-sql):
#   data/                 NovaSky-AI/SkyRL-SQL-653-data-newfmt (train.parquet)
#   db/                   seeklhy/OmniSQL-datasets data.zip, unzipped (~22 GB zip,
#                         ~50 GB unzipped; SynSQL-2.5M + Spider databases)
#   hf/                   HF cache with Qwen/Qwen2.5-Coder-7B-Instruct (~15 GB)
# Safe to re-run: finished steps are skipped.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${SKYRL_SQL_DIR:-$SCRATCH/skyrl-sql}"
MODEL="${SKYRL_SQL_MODEL:-Qwen/Qwen2.5-Coder-7B-Instruct}"
mkdir -p "$DIR/data" "$DIR/db" "$DIR/hf"
export HF_HOME="$DIR/hf"

module load python/3.13-26.8.1
cd "$REPO"
# vLLM wheels lag new Python releases, so .venv-collect uses a standalone
# Python 3.12 fetched by uv (installed in user site; no admin rights needed).
python3 -m pip install -q --user uv
UV="$(python3 -m site --user-base)/bin/uv"
[[ -x .venv-collect/bin/python ]] || "$UV" venv -q --python 3.12 .venv-collect
VIRTUAL_ENV="$REPO/.venv-collect" "$UV" pip install -q \
    -e upstream/tvclient -e '.[collect,eval]' huggingface_hub
.venv-collect/bin/python -c "import vllm, fuzzytv; print('vllm', vllm.__version__)"

dl() { .venv-collect/bin/python - "$@" <<'PY'
import sys
from huggingface_hub import hf_hub_download, snapshot_download
kind, repo, target = sys.argv[1:4]
if kind == "file":
    print(hf_hub_download(repo, sys.argv[4], repo_type="dataset", local_dir=target))
elif kind == "dataset":
    print(snapshot_download(repo, repo_type="dataset", local_dir=target))
else:
    print(snapshot_download(repo))
PY
}

[[ -n "$(find "$DIR/data" -name 'train.parquet' -print -quit)" ]] \
    || dl dataset NovaSky-AI/SkyRL-SQL-653-data-newfmt "$DIR/data"

if [[ -z "$(find "$DIR/db" -maxdepth 3 -type d -name SynSQL-2.5M -print -quit)" ]]; then
    [[ -f "$DIR/db/data.zip" ]] || dl file seeklhy/OmniSQL-datasets "$DIR/db" data.zip
    (cd "$DIR/db" && unzip -q data.zip && rm data.zip)
fi

dl model "$MODEL" ignored

DB_ROOT="$(dirname "$(find "$DIR/db" -maxdepth 3 -type d -name SynSQL-2.5M -print -quit)")"
echo
echo "train.parquet: $(find "$DIR/data" -name train.parquet -print -quit)"
echo "database root: $DB_ROOT"
ls "$DB_ROOT"
echo "Setup done. Submit: sbatch -A <account>_g -q regular slurm/collect_skyrl_sql.sbatch"
