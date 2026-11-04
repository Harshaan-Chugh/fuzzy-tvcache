# Evaluation plan (owner: Rohan)

## Modes

| Mode | Behavior |
|---|---|
| `none` | Execute every call. No TVCache. |
| `exact` | Unmodified upstream `TvcacheToolExecutor`. |
| `canonical` | Canonicalized call strings in the key. Full stateful-chain history is still required. |
| `relevant_state` | Canonical call plus relevant-state identity (fuzzy index), with fallback to exact. |

The mode is chosen explicitly (config/CLI). `exact` must be the upstream behavior with no changes.

## Metrics (denominators fixed up front)

- **Hit rate** = hits / total tool calls (`executor.total_calls`). Reported per mode, with the
  `relevant_state` hits split into exact and fuzzy.
- **Additional fuzzy hits** = fuzzy hits that `exact` mode would have missed on the same trace.
- **False-hit rate** = false hits / *validated* fuzzy hits. A false hit is a reused value that
  differs from executing the call in the **requesting rollout's own state** (not the state that
  produced the cached value). Compare the full value string (stdout, stderr, exit status, as
  encoded by the workload's `Sandbox.execute`).
- Matching overhead and state-tracking overhead per call; end-to-end tool time; rollout and step
  time; RL reward (late Nov).

## Validation

The live cache-backed sandbox may lag logical history after exact or fuzzy hits.
Do not assume it is materialized at the requesting rollout's current state.

The controlled validation harness maintains a separate reference sandbox with
equivalent initial state. It executes every original logical call in order,
including calls served by exact and fuzzy hits, without consulting cache keys
or cached snapshots. Compare the complete returned value strings.

`ReferenceValidator` validates every call supplied to it and rejects skipped
history. It records mismatches before raising an assertion failure. All fuzzy
hits in correctness tests are validated; sampling is deferred to the workload
evaluation harness.

Report policy execution counts separately from reference executions.
`CacheStats` counts completed logical exact hits, fuzzy hits, misses, and
secondary-cache errors. Upstream `total_executions` includes physical replay.
An exact miss followed by a fuzzy hit is an additional fuzzy hit.

Default `pytest -q` runs server-free tests and skips server integration tests.
Use a dedicated running server for:

```bash
pytest -q -s --run-tvcache-integration tests/integration
```

On Perlmutter, `slurm/cache-integration.sbatch` builds and starts a dedicated
server on a compute node, runs tests, and writes logs under `$SCRATCH`.

The controlled harness covers uncached reference execution, upstream exact
lookup, and relevant-state lookup. The standalone `canonical` evaluation mode,
representative workload benchmarks, sampling, and RL measurements remain
subsequent evaluation work.

Workloads must be reproducible: fixed traces or seeds, stored under `eval/workloads/` if small,
otherwise on `$SCRATCH`.
