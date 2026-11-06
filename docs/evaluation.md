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

Workloads must be reproducible: fixed traces or seeds, stored under `eval/workloads/` if small,
otherwise on `$SCRATCH`.

## Workload evaluation harness (`src/fuzzytv/eval/`)

Usage is in [eval/README.md](../eval/README.md). The design decisions:

**Traces.** Each `eval/workloads/*.json` trace holds tasks (initial state plus a group of
rollouts, as in GRPO), generated from a seed. Rollouts follow a per-task plan with seeded
perturbations:
- spelling variants (SQL case, whitespace, aliases; `./a`, `a`, `/app/a`);
- inserted irrelevant writes;
- inserted relevant writes and unknown commands;
- dropped steps and early stops.

Calls use the dict schema in `fuzzytv/eval/calls.py`: `{"tool": "sql", "query": ...}` and
`{"tool": "bash", "command": ...}`. This is our proposal until the production `to_dict()` is
confirmed.

**Sandboxes.** SQL runs on in-memory SQLite. Terminal commands run in an emulated shell over a
virtual filesystem, which is deterministic and does no host I/O. Values are
`exit=<code>\n<output>` and call-local, so `call_local_results=True` holds. Neither sandbox
replaces the real container backend; they make the controlled numbers exact and reproducible.

**Canonicalizers.**
- `oracle`: what a canonicalizer following `docs/interfaces.md` exactly would return. It's known
  by construction from the generator, so no parsing is involved. It's the upper bound for
  Kevin's and Etienne's implementations and is usable before they exist.
- `loose`: the proposal's least-strict level. It classifies read vs. write only and ignores read
  dependencies. It is unsafe on purpose, to measure the cost side and to show that the validator
  catches false hits.
- `real`: dispatches by tool to `fuzzytv.sql.SqlCanonicalizer` / `fuzzytv.fs.FsCanonicalizer`.

**Modes.**
- `canonical:<c>` keys each call on `{"fuzzytv_canonical_v1": canonical}` when one exists,
  otherwise on the upstream serialization. The full stateful history is still required, and the
  original call is what executes.
- `relevant_state:<c>` is `RelevantStateExecutor`.

**Read-only hints.** Upstream `will_mutate_state()` defaults to True, so every read joins the
stateful chain and is replayed on the next miss. `--read-only-hints on` marks the oracle's
read-only calls as stateless, modeling a tool that declares its reads. Results differ a lot
between the two settings, so we report both.

**Validation.** Sampling is per rollout, seeded, at `--sample-rate`. A sampled rollout replays
every original call in a fresh reference sandbox. Every returned value from that rollout (exact,
fuzzy or executed) is compared with the full reference value. False-hit rate = false fuzzy hits ÷
validated fuzzy hits, with a 95% Wilson interval. Exact-hit and executed-call mismatches are also
reported and must be 0 (they would indicate nondeterministic tools). Reference executions are
counted separately from policy executions.

**Backends.** `--backend memory` (default) is an in-process stand-in for the Rust server's trie
semantics. `tests/integration/test_eval_backend_parity.py` asserts call-for-call identical
decisions against a real server. It passed on Perlmutter (job 60884044, 2026-11-05) in all 24
configurations: sql/fs × exact/canonical/relevant_state × snapshots always/never × hints
off/on. Memory-backend hit, fuzzy-hit and false-hit counts therefore carry over to the real
server; timings do not.

**Time.**
- Wall time on the memory backend measures our Python overhead only.
- Policy sandboxes sleep `cost × --latency-scale` per operation, so the adaptive snapshot policy
  sees realistic relative costs.
- `modeled_tool_seconds` = count of each operation × `CostModel`. It is a projection, and only
  as good as the cost model. Real step-time numbers need the container backend.

## Baseline results (controlled workloads; confirmed on the real server; 2026-11-05)

`python -m fuzzytv.eval run eval/workloads/sql-small.json eval/workloads/fs-small.json`,
every rollout validated, adaptive snapshots. Across three runs, hit and false-hit counts were
identical. Execution ratios varied by ±0.01, because adaptive snapshotting reacts to measured
time. Use `--snapshot always` or `--snapshot never` for exactly reproducible executions.

The same run against the real TVCache server on a Perlmutter CPU node (`slurm/eval.sbatch`,
job 60884044) reproduced every hit, fuzzy-hit and false-hit count in the table below exactly.
Execution counts differed by at most 2 (same timing wobble). Modeled tool time was higher
there for cached modes, e.g. sql `exact` 142.8 s vs. 122.6 s. Decisions were identical, so the
gap comes from the adaptive policy taking more snapshots: the simulated sleeps measure
differently on that machine. Modeled time is therefore machine-dependent and is not reported as
a result.

| workload | hints | mode | hit rate | fuzzy hits | false / validated fuzzy | executions vs. `none` |
|---|---|---|---|---|---|---|
| sql | off | exact | 5.9% | 0 | — | 1.00 |
| sql | off | canonical:oracle | 9.3% | 0 | — | 1.00 |
| sql | off | relevant_state:oracle | 45.2% | 207 | 0 / 207 | 0.91 |
| sql | off | relevant_state:loose | 56.9% | 269 | **44 / 269** | 0.87 |
| sql | on | exact | 20.1% | 0 | — | 0.83 |
| sql | on | canonical:oracle | 30.0% | 0 | — | 0.75 |
| sql | on | relevant_state:oracle | 47.8% | 180 | 0 / 180 | 0.55 |
| sql | on | relevant_state:loose | 59.6% | 248 | **44 / 248** | 0.43 |
| fs | off | exact | 2.2% | 0 | — | 1.00 |
| fs | off | canonical:oracle | 5.8% | 0 | — | 0.99 |
| fs | off | relevant_state:oracle | 28.7% | 205 | 0 / 205 | 0.98 |
| fs | off | relevant_state:loose | 54.8% | 406 | **92 / 406** | 0.89 |
| fs | on | exact | 11.0% | 0 | — | 0.91 |
| fs | on | canonical:oracle | 18.8% | 0 | — | 0.84 |
| fs | on | relevant_state:oracle | 31.2% | 178 | 0 / 178 | 0.71 |
| fs | on | relevant_state:loose | 56.8% | 383 | **92 / 383** | 0.45 |

Baseline findings:

1. **Hit rate is not saved work.** With upstream's default (every call stateful), fuzzy hits
   barely reduce physical executions. On fs, a 29% hit rate cuts executions by only 2%, because
   a later miss replays the skipped reads. Relevant-state matching pays off mainly together with
   read-only hints or snapshots. This is why the report must show executions and time, not only
   hit rate.
2. **SQL output carries column labels.** SQLite labels unaliased result columns with the
   select-list text as typed (`SUM(amount)` vs. `sum(amount)`). Rows were identical but values
   differed, which produced false hits until the oracle separated these spellings. The SQL
   canonicalizer must preserve select-list text (or its case) unless the column is aliased.
   This is encoded in `test_sql_output_column_labels_are_preserved`, and `fuzzytv.sql` now does
   this (see the midpoint results).
3. **Ignoring read dependencies is unsafe.** The loose level adds 12–26 points of hit rate but
   16–24% of its fuzzy hits are wrong. The spec-level oracle has 0 false hits in 770 validated
   fuzzy hits. Upper 95% bound per cell is about 2%.

## Midpoint results: real canonicalizers (2026-11-06)

Both teammate canonicalizers are on `main` (`fuzzytv.sql`, PR #4; `fuzzytv.fs`, PR #5). Two
gaps in the SQL canonicalizer found by the harness are fixed on this branch: result column labels
are preserved (the two failing contract tests now pass) and `PRAGMA table_info(t)` is a read of
`t` instead of unknown. Before the PRAGMA fix, `relevant_state:real` got 4 fuzzy hits on
`sql-small` (405 misses were caused by PRAGMA disabling fuzzy lookup); after, 184.

Command (memory backend, adaptive snapshots, every rollout validated):

```bash
python -m fuzzytv.eval run eval/workloads/sql-small.json eval/workloads/fs-small.json \
    --modes none exact canonical:oracle canonical:real relevant_state:oracle \
    relevant_state:real relevant_state:loose relevant_state:real-loose \
    --read-only-hints both --name midpoint --plot
```

| workload | hints | mode | hit rate | fuzzy hits | false / validated fuzzy (95% CI) | executions vs. `none` | analysis µs/call |
|---|---|---|---|---|---|---|---|
| sql | off | exact | 5.9% | 0 | — | 1.00 | — |
| sql | off | canonical:real | 7.0% | 0 | — | 1.00 | 1068 |
| sql | off | relevant_state:oracle | 45.2% | 207 | 0 / 207 (0–1.8%) | 0.91 | 10 |
| sql | off | **relevant_state:real** | **40.8%** | 184 | **0 / 184** (0–2.0%) | 0.92 | 999 |
| sql | off | relevant_state:real-loose | 54.5% | 256 | 45 / 256 (13–23%) | 0.87 | 945 |
| sql | on | exact | 20.1% | 0 | — | 0.83 | — |
| sql | on | canonical:real | 28.1% | 0 | — | 0.77 | 1001 |
| sql | on | relevant_state:oracle | 47.8% | 180 | 0 / 180 (0–2.1%) | 0.55 | 9 |
| sql | on | **relevant_state:real** | **44.8%** | 151 | **0 / 151** (0–2.5%) | 0.58 | 854 |
| sql | on | relevant_state:real-loose | 57.1% | 231 | 46 / 231 (15–26%) | 0.46 | 918 |
| fs | off | exact | 2.2% | 0 | — | 1.00 | — |
| fs | off | canonical:real | 5.5% | 0 | — | 1.00 | 46 |
| fs | off | relevant_state:oracle | 28.7% | 205 | 0 / 205 (0–1.8%) | 0.98 | 10 |
| fs | off | **relevant_state:real** | **26.4%** | 186 | **0 / 186** (0–2.0%) | 0.99 | 46 |
| fs | off | relevant_state:real-loose | 52.3% | 386 | 87 / 386 (19–27%) | 0.90 | 48 |
| fs | on | exact | 11.0% | 0 | — | 0.91 | — |
| fs | on | canonical:real | 18.2% | 0 | — | 0.85 | 45 |
| fs | on | relevant_state:oracle | 31.2% | 178 | 0 / 178 (0–2.1%) | 0.71 | 9 |
| fs | on | **relevant_state:real** | **28.8%** | 160 | **0 / 160** (0–2.3%) | 0.73 | 54 |
| fs | on | relevant_state:real-loose | 54.3% | 364 | 87 / 364 (20–29%) | 0.48 | 45 |

Exact-hit and executed-call mismatches are 0 in every cell. Figures:
`docs/figures/midpoint/{hit_rates,false_hits,executions}.png`.

Findings:

1. **The real canonicalizers are safe on these workloads.** 0 false hits in 681 validated fuzzy
   hits across both workloads and both hint settings (pooled 95% upper bound 0.6%).
2. **They reach about 90% of the oracle's fuzzy hits.** SQL: 184 of 207 (hints off). fs: 186 of
   205. The remaining SQL gap is `sqlite_master` queries (27 misses), which read every table's
   schema and have no state key yet. The fs gap is shell control operators and brace expansion
   (`a && b`, `{a,b}`), which the fs canonicalizer rejects by design.
3. **The `-loose` variant of the real canonicalizers is as unsafe as the oracle's.** It drops
   read dependencies and gets 18–24% false hits, so read-dependency tracking, not
   canonicalization, is what makes fuzzy reuse correct.
4. **Fuzzy matching needs read-only declarations to save work.** Without hints, a 41% hit rate
   cuts SQL executions by 8% and a 26% hit rate cuts fs executions by 1%. With hints,
   `relevant_state:real` cuts executions by 42% (SQL) and 27% (fs), vs. 17% and 9% for `exact`.
5. **SQL analysis costs about 1 ms per call** (SQLGlot parse and label recovery); fs about 50 µs.
   This is small next to container tool latency but not next to an in-process SQLite query, so
   the report should give overhead per call next to tool time.

### Coverage on real benchmark text

`python -m fuzzytv.eval.coverage` runs the real canonicalizers over benchmark text without a
model. Gold queries and reference solutions are a proxy for model-written calls.

| Benchmark | Calls | Fuzzy-eligible reads | Known writes | Fall back to exact | Top reasons |
|---|---|---|---|---|---|
| SkyRL-SQL gold queries (653) | 653 | 96.6% | 0% | 3.4% | volatile (`'now'`, `random()`) |
| terminal-bench easy+medium `solution.sh` lines (146 tasks) | 2041 | 15.2% | 1.4% | 83.4% | `$VAR` expansion 342, `&&`/`;`/pipes 209, control chars 112, globs 85, `curl` 61, `apt-get` 51, `if/fi` 50, `mkdir` 36 |

Most SQL the benchmark uses is in the supported subset. Terminal solutions are mostly setup and
scripting, which falls back to exact, so fuzzy gains on terminal-bench will come from the agent's
exploratory reads (`cat`, `ls`), not from its writes. Supporting `mkdir`, `$VAR` with a known
environment, and `&&` sequences of supported commands are the largest next steps for fs coverage.

Open evaluation work:
- confirm the `:real` rows on the real server (`slurm/eval.sbatch` now includes them, and the
  parity test covers them);
- collect SkyRL-SQL rollouts on Perlmutter GPUs, and terminal-bench rollouts on a Docker host;
- verl RL runs (reward, step time).

## Real workloads (from the TVCache paper)

The TVCache paper (arXiv 2602.10986, ICML 2026) evaluates on terminal-bench, SkyRL-SQL and
EgoSchema. Our proposal covers the first two.

| | Terminal | SQL |
|---|---|---|
| Benchmark | terminal-bench easy (51) and medium (95) | SkyRL-SQL, 653 questions |
| Our copy | `container-manager`: `tasks_easy_network_skip` (51), `tasks_medium_network_skip` (95) | `NovaSky-AI/SkyRL-SQL-653-data-newfmt` + `seeklhy/OmniSQL-datasets` |
| Model | Qwen3-4B-Instruct-2507 / Qwen3-14B | Qwen2.5-Coder-7B-Instruct |
| Rollouts per prompt | 8 | 5 |
| Tool call | Terminus-2 JSON keystroke batches into tmux | one `<sql>` per turn, at most 6 turns |
| State | every call treated as mutating | read-only (rolled-back transactions) |
| Paper hit rate | 14–25% | 33% |

**SkyRL-SQL collection** (`fuzzytv.eval.skyrl_sql`) reproduces SkyRL's text2sql environment and
training recipe. Sampling, stop strings, observation text and the turn/turns-left rules are taken
from `skyrl-gym` source. It is inference only. `--passes k` re-samples each question to approximate
repeated epochs; the policy does not change between passes as it would during training. Run:
`bash slurm/setup_skyrl_sql.sh` on a login node, then
`sbatch -A <account>_g -q regular slurm/collect_skyrl_sql.sbatch`. Estimate: about 15–30 GPU-node
minutes per pass. Not yet run on Perlmutter.

Notes from SkyRL's code that matter for caching:
- **Observation row order varies between processes.** Observations print a `frozenset` of rows,
  so the same query in the same state can print differently in different processes. Replay
  compares sorted rows.
- **The turns-left reminder is not part of the result.** Each observation ends with "You have N
  turns left". If a cached value included it, reuse at a different turn would be wrong. Our
  replay caches only the query result.
- **Because every call is stateless, relevant-state tracking adds nothing here.** Only
  canonicalization can add hits beyond `exact`.

**terminal-bench.** `rlhf_server/server.py` in container-manager exposes `/start`, `/execute`,
`/get_pane_logs`, `/fork`, `/test` and `/stop`.
- **The full pane comes back.** `/execute` returns only a status; output comes from
  `/get_pane_logs`, which returns the **entire tmux pane**, i.e. all earlier commands. A cached
  value is therefore not call-local unless the integration extracts per-command output. This
  confirms the risk in `docs/architecture.md`.
- **Docker is required.** Tasks need Docker Compose, which Perlmutter compute nodes lack (the
  paper used its own servers). Collection needs a Linux host with Docker and is not started.
