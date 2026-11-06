# Roadmap

| Week | Milestone | Owners |
|---|---|---|
| Oct 26–Nov 1 | Repo and Perlmutter setup ✅. Upstream read-through ✅. **Shared interface** (`interfaces.py`). Supported-op list. Baseline run instructions. | Harshaan (interface), all |
| Nov 2–8 | First SQL and fs canonicalizers, with equivalent and non-equivalent test cases ✅ (PR #4, #5). Baseline harness ✅ (PR #2). | Kevin, Etienne, Rohan |
| Nov 9–15 | State identity and fuzzy key ✅. Fuzzy-aware executor wrapper ✅ (PR #1). Demonstrate reuse across irrelevant changes and invalidation on relevant ones ✅ (real canonicalizers: 0 false hits in 681 validated fuzzy hits, `docs/evaluation.md`). | Harshaan |
| Nov 16–22 | Validation harness ✅. All four modes compared ✅ (controlled workloads). False-hit rate and overhead measured ✅. Real-server confirmation of `:real` modes ✅ (job 60891246). | Rohan, Harshaan |
| Nov 23–29 | Representative workloads. Small RL runs with the advisor's setup (verl). | All |
| Nov 30–Dec 4 | Correctness pass. Perf analysis. Upstream PR. Figures. Report. | All |

## Next steps for Rohan

1. ✅ Eval harness (`src/fuzzytv/eval/`): traces, four modes, sampled validation, metrics, plots.
2. ✅ Canonicalizer acceptance tests (skip until `sql/`, `fs/` land).
3. ✅ Run `slurm/eval.sbatch`: server parity passed and real-server baseline matches (job 60884044).
4. ✅ `:real` modes for both canonicalizers, compared against `:oracle` (midpoint results).
   Fixed SQL column labels and `PRAGMA table_info` in `fuzzytv.sql`.
5. ✅ SkyRL-SQL rollout collector and Perlmutter jobs; canonicalizer coverage on real benchmark
   text (`python -m fuzzytv.eval.coverage`).
6. ✅ Server confirmation of `:real` rows (job 60891246). Run
   `slurm/collect_skyrl_sql.sbatch` (GPU). Terminal-bench rollouts need a Linux Docker host.
7. RL runs with verl.

## Next steps for Harshaan

1. ✅ Design `src/fuzzytv/interfaces.py` (see `docs/interfaces.md` open questions).
2. ✅ Pick a state-identity scheme (`docs/state-tracking.md`).
3. ✅ Mock-state vertical slice with tests: identical, reformatted, irrelevant mutation, relevant
   mutation, divergent independent histories, unknown deps, cross-task isolation
   (`tests/unit/test_mock_rollouts.py`, `test_state_tracker.py`, PR #1).
4. A state key for SQL schema reads (`sqlite_master`); it is the largest remaining SQL miss
   reason in the evaluation.
5. Fork `tvcache-rust` and `tvclient` on GitHub and re-point the submodules (README).
