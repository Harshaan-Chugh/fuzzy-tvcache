# Evaluation (owner: Rohan)

- `workloads/` — reproducible workload definitions (tool-call traces, task sets). Small
  fixtures only; large datasets live on `$SCRATCH`.
- `scripts/` — runners for the four cache modes, validation re-execution, plotting.

Mode definitions, metrics, and the false-hit definition are in `docs/evaluation.md`.
All outputs go to `$FUZZYTV_OUT` (default `$SCRATCH/fuzzy-tvcache`), never into git.
