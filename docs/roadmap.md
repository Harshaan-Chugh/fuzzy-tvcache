# Roadmap

| Week | Milestone | Owners |
|---|---|---|
| Oct 26–Nov 1 | Repo and Perlmutter setup ✅. Upstream read-through ✅. **Shared interface** (`interfaces.py`). Supported-op list. Baseline run instructions. | Harshaan (interface), all |
| Nov 2–8 | First SQL and fs canonicalizers, with equivalent and non-equivalent test cases. Baseline harness skeleton. | Kevin, Etienne, Rohan |
| Nov 9–15 | State identity and fuzzy key. Fuzzy-aware executor wrapper. Demonstrate reuse across irrelevant changes and invalidation on relevant ones. | Harshaan |
| Nov 16–22 | Validation harness. All four modes compared. False-hit rate and overhead measured. | Rohan, Harshaan |
| Nov 23–29 | Representative workloads. Small RL runs with the advisor's setup (verl). | All |
| Nov 30–Dec 4 | Correctness pass. Perf analysis. Upstream PR. Figures. Report. | All |

## Next steps for Harshaan

1. Design `src/fuzzytv/interfaces.py` (see `docs/interfaces.md` open questions).
2. Pick a state-identity scheme (`docs/state-tracking.md`).
3. Mock-state vertical slice with tests: identical, reformatted, irrelevant mutation, relevant
   mutation, divergent independent histories, unknown deps, cross-task isolation.
4. Fork `tvcache-rust` and `tvclient` on GitHub and re-point the submodules (README).
