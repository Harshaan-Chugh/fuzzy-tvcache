# Evaluation (owner: Rohan)

Code: `src/fuzzytv/eval/`. Definitions and findings: [docs/evaluation.md](../docs/evaluation.md).

- `workloads/` holds small, committed, reproducible traces: `sql-small.json` (SQLite agent)
  and `fs-small.json` (terminal agent). Each has 6 tasks × 8 rollouts. Large traces go on
  `$SCRATCH`.
- Results go to `$FUZZYTV_OUT/eval/<name>` (default `$SCRATCH/fuzzy-tvcache`, else
  `./results`), never into git.

## Quick start (no server, about 30 s)

```bash
pip install -e upstream/tvclient -e '.[dev,eval]'
python -m fuzzytv.eval run eval/workloads/sql-small.json eval/workloads/fs-small.json \
    --name baseline --plot
```

This runs the five default modes (`none`, `exact`, `canonical:oracle`,
`relevant_state:oracle`, `relevant_state:loose`), each with read-only hints off and on. It
validates every rollout against an independent reference sandbox. It writes:

| File | Contents |
|---|---|
| `summary.json`, `summary.csv` | One row per (workload, hints, mode): hit rate, exact/fuzzy split, validated and false fuzzy hits with 95% CI, physical executions, sandbox starts/forks/restores, analysis µs per call, modeled tool time |
| `calls.jsonl` | One row per call: source (`exact`/`fuzzy`/`execute`), executions triggered, validation result, oracle eligibility, decision reason |
| `mismatches.jsonl` | Every call whose returned value differed from the reference, with both values |
| `hit_rates.png`, `false_hits.png`, `executions.png` | Report figures |

## Commands

```bash
# New trace (deterministic in --seed; perturbation knobs: --p-variant --p-noise --p-drop --p-stop)
python -m fuzzytv.eval generate --kind fs --seed 1 --tasks 20 --rollouts 16 -o $SCRATCH/fs-large.json

# Choose modes. kind[:canonicalizer]: kinds none|exact|canonical|relevant_state;
# canonicalizers oracle|loose|real|real-loose
python -m fuzzytv.eval run eval/workloads/sql-small.json \
    --modes exact canonical:real relevant_state:real relevant_state:oracle

# Against a real server instead of the in-memory one
python -m fuzzytv.eval run ... --backend server --host 127.0.0.1 --port 7878

# Sampling, snapshot policy, cost model
python -m fuzzytv.eval run ... --sample-rate 0.1 --snapshot adaptive \
    --latency-scale 0.01 --cost '{"execute": 0.2, "start": 5}'

python -m fuzzytv.eval plot results/eval/baseline
```

`:real` modes use `fuzzytv.sql.SqlCanonicalizer` and `fuzzytv.fs.FsCanonicalizer`. They are
skipped with a message until those exist. When only one exists, calls for the other tool
analyze as unknown.

## On Perlmutter

```bash
sbatch -A <account> -q <qos> -C cpu slurm/eval.sbatch
```

The job builds the pinned server and runs the backend-parity integration test (memory and
server must make identical decisions). It then runs every mode against the real server and
plots the results. Logs go to `$SCRATCH/fuzzy-tvcache/eval-jobs/<jobid>`.
