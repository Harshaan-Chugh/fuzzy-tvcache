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

Sample fuzzy hits at a configurable rate and re-execute them in the requesting rollout's sandbox.
That sandbox is already fast-forwarded to the correct state, and the fuzzy-hit call is
non-mutating, so re-executing it is safe. Log mismatches with the analysis and fallback reasons.

Workloads must be reproducible: fixed traces or seeds, stored under `eval/workloads/` if small,
otherwise on `$SCRATCH`.
