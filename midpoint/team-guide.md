# FuzzyTVCache: Team Guide to the Midpoint

Team 30, as of 2026-11-07.

## How to use this guide

Read it top to bottom once and each of us can explain every part of the project, not only our own. Sections 1–2 give the project from zero. Sections 3–8 cover one component each, with its owner, the key idea, a worked example and what to say about it. Sections 9–11 cover results, real workloads and infrastructure. Section 12 has likely questions with answers.

Where each section shows up on the poster (`midpoint-poster.pdf`):

| Poster panel | Guide section |
| --- | --- |
| Opening question (the line under the title) | 1, 2 |
| Motivation and Figure 1 (TVCache tree vs. our lookup, rollouts A and B) | 1, 2, 4, 5 |
| Approach, steps 1–2 (canonicalize; reads and writes) | 3, 6, 7 |
| Approach, steps 3–4 (versions; exact then fuzzy lookup) | 4, 5 |
| Evaluation setup | 8 |
| Results: Figure 2 (hit rates), Figure 3 (wrong hits), Figure 4 (executions saved) | 8, 9 |
| Real rollouts: SkyRL-SQL table and coverage bars | 10 |
| Next steps | 13 |
| References and code link (footer) | 11 |

All numbers come from `docs/evaluation.md`; code paths are relative to the repo root.

## 1. The project in one page

**One sentence:** we make TVCache, a cache for agent tool calls during RL training, reuse results in many more cases without ever returning a wrong result.

**The setting.** In RL for tool-using LLM agents, the trainer samples many rollouts of the same task (for example 5 attempts at the same SQL question). Each rollout is a sequence of tool calls: SQL queries, shell commands. Rollouts of the same task repeat many calls, and executing tools is slow, so caching their results saves time.

**What TVCache does.** It stores results in a tree keyed on the exact text of each call and of every earlier state-changing call in the rollout. A result is reused only if the whole prefix matches exactly.

**What it misses.**

- *Spelling differences:* `select name from USERS` and `SELECT name FROM users` mean the same thing but are different keys.
- *Irrelevant history:* a read of table `users` after an insert into `orders` can't reuse a result from a rollout with a different insert, even though `users` is identical in both.

**Our idea.** For each call, work out (a) a canonical form, so equivalent calls get the same text, and (b) exactly which pieces of state it reads and writes. Track a version for each piece of state. A read can reuse a cached result when its canonical form matches and every piece of state it reads has the same version. Anything we don't fully understand falls back to normal TVCache.

**Headline results at the midpoint** (controlled workloads, every reused value checked):

- 0 wrong results in 681 fuzzy hits from our real canonicalizers.
- About 90% of the fuzzy hits a perfect canonicalizer (the oracle) would get.
- With read-only hints, 42% fewer SQL executions than no cache, vs. 17% for TVCache.
- On real SkyRL-SQL rollouts, unmodified TVCache gets a 33.2% hit rate, matching the TVCache paper's 33%. That confirms our harness measures what the paper measured.

**Who did what.**

| Member | Component | Code |
| --- | --- | --- |
| Harshaan Chugh | Shared interfaces, relevant-state tracking, cache integration | `src/fuzzytv/interfaces.py`, `state/`, `cache/` |
| Kevin Rodriguez | SQL canonicalizer | `src/fuzzytv/sql/` |
| Etienne Sasenarine | Filesystem/shell canonicalizer | `src/fuzzytv/fs/` |
| Rohan Shankar | Evaluation harness, validation, real-workload collection | `src/fuzzytv/eval/`, `slurm/` |

## 2. Background: rollouts, tool calls and TVCache

**Agentic RL.** The policy (an LLM) gets a task, writes a tool call, sees the result (the *observation*), and repeats until it answers. One such attempt is a *rollout*. GRPO-style training samples several rollouts per task and compares their rewards, so the same task is run many times per step. Tool execution (a database query, a command in a container) can dominate step time.

**Why caching is tricky.** Many tools have state. If rollout A ran `INSERT INTO orders …` and rollout B didn't, a later `SELECT * FROM orders` returns different results. A cache that ignores history returns wrong observations, which corrupts training. So any reuse must be *correct*: the cached value must equal what executing the call would have returned in this rollout.

**TVCache** (arXiv 2602.10986; Rust server `upstream/tvcache-rust`, Python client `upstream/tvclient`) handles this conservatively:

1. Each call has `will_mutate_state()`, which defaults to `True`.
2. The cache key for a call is the list of all earlier *mutating* calls in the rollout, plus the call itself, each serialized to JSON text.
3. The server stores these keys as a tree (the *tool call graph*). `find(path)` returns the value on an exact full-path match. Otherwise it returns the deepest ancestor that holds a sandbox snapshot, so a miss can fork that snapshot instead of replaying from scratch.
4. On a miss, the client executes the call (after fast-forwarding a sandbox through the missing history) and inserts the result.

The server never parses a call: it is a string trie. Its README lists "intelligent lookup to identify equivalent states" as a roadmap item, which is exactly what we build.

**The gap we fill.** TVCache is correct but literal: any spelling difference or any extra unrelated write breaks the match. We add a second lookup that runs only after TVCache's exact lookup misses.

## 3. Shared interfaces (Harshaan)

`src/fuzzytv/interfaces.py` is the contract every component meets. It lets Kevin, Etienne and Harshaan work independently: the canonicalizers only describe one call; the tracker only combines descriptions.

**`Canonicalizer.analyze(call, ctx) -> CallAnalysis`** looks at ONE call and says what it means. It is pure: no I/O, no sandbox, never raises.

**`CallContext`** is what the canonicalizer may know besides the call: `cwd` (the shell's working directory) and `database` (SQL's default database).

**`CallAnalysis`** fields:

| Field | Meaning |
| --- | --- |
| `canonical` | Text that is identical for equivalent calls; `None` if we can't produce one |
| `reads` | State keys the call may read |
| `writes` | State keys the call may change |
| `reads_unknown` | May read state not listed (including time, randomness, network) |
| `writes_unknown` | May write state not listed |
| `reason` | Why the call isn't eligible, for logs and evaluation |

A call is **fuzzy-eligible** only if it has a canonical form, no unknown reads or writes, and no writes at all. Writes always execute in the rollout's own sandbox.

**State keys** name pieces of state:

- `sql:table:<db>.<table>`: rows and schema of one table
- `fs:file:<abs path>`: contents and existence of a file
- `fs:dir:<abs path>`: the set of names in a directory
- `fs:cwd`: the shell's working directory

**Fail closed.** `unknown(reason)` returns an analysis that may read and write anything. Guessing can produce a wrong cached result; saying "unknown" only costs a cache hit. This is the project's central rule: *correctness over hit rate*.

**How to explain it:** "Every canonicalizer answers three questions about a call: what is its normal form, what does it read, what does it write. If it can't answer, it says unknown and we fall back to TVCache."

## 4. Relevant-state tracking (Harshaan)

`src/fuzzytv/state/tracker.py`: one `StateTracker` per rollout. It turns the stream of `CallAnalysis` objects into a version for every piece of state, and into a fuzzy key for each eligible read.

**Versions are lineage hashes, not counters.** All hashes are SHA-256 of sorted JSON, with a separate namespace per kind:

- A piece of state nobody has written has its initial version: `H(initial, task, component)`.
- When an understood write runs, each piece of state it writes gets `H(write, previous version, canonical write, sorted (read key, version) pairs)`.
- An eligible read gets the fuzzy key `H(key, task, canonical call, sorted (read key, version) pairs)`.

Why lineage and not a counter: two rollouts that each made one *different* insert into `orders` must not both be "orders version 1". Hashing the write itself makes the versions differ. Including the write's reads handles copies: `INSERT INTO a SELECT * FROM b` makes `a` depend on `b`'s version.

**Worked example** (poster Figure 1). Rollout A: `INSERT INTO orders …(7,'pen')` then `SELECT name FROM users WHERE id=3`. Rollout B: a different insert into `orders`, then `select name from USERS where id=3`.

- The insert changes the version of `sql:table:main.orders` only.
- The SELECT reads only `sql:table:main.users`, which is still at its initial version in both rollouts.
- Both SELECTs canonicalize to the same text, so they get the same fuzzy key, and B reuses A's result.
- If B had instead updated `users`, `users`' version would differ, the key would differ, and B would miss. That's correct.

**When tracking turns off.** `observe()` permanently disables fuzzy lookup for the rest of the rollout on: an unknown write, a write with no canonical text, or a write whose reads are unknown. After that, only exact TVCache is used. A read we don't understand is just ineligible; it doesn't disable anything, because it changes no state.

## 5. Cache integration (Harshaan)

`src/fuzzytv/cache/`: `RelevantStateExecutor` subclasses upstream's `TvcacheToolExecutor`. It adds the fuzzy lookup and leaves all execution, sandbox forking and replay to upstream code. That keeps `exact` mode byte-for-byte identical to TVCache.

**One call, step by step** (`executor.py`, `lookup_or_execute`):

1. Check that the history is a sequential append of the previous call; if not, disable fuzzy lookup.
2. Ask the canonicalizer for the call's `CallAnalysis`, using the logical `CallContext`. Any exception becomes `unknown`.
3. Ask the tracker for the fuzzy key (only if the workload confirmed results are *call-local*, see below).
4. Run upstream's lookup. Our `LookupAdapter.find` (`adapters.py`) intercepts it: try **exact** TVCache first. On an exact miss with a key, look the key up in the **fuzzy index**. On a fuzzy hit, release the exact lookup's sandbox lease and return the value without a sandbox. Upstream then leaves the live sandbox behind, exactly as it does for an exact hit.
5. On a miss, upstream executes as usual. We then observe the call in the tracker and store the value under the fuzzy key.

**The fuzzy index** (`index.py`) reuses the TVCache server itself: each entry is a one-element path in a reserved task namespace, `H(task, initial-state id, policy version)`. Values carry a small JSON envelope with the key, so a mismatched entry is rejected. No server change is needed.

**Safety guards worth knowing:**

- `call_local_results` must be set explicitly. If a tool's output includes earlier history (terminal-bench's server returns the whole tmux pane), a cached value can't be reused across histories, so fuzzy lookup stays off.
- If the canonicalizer says a call writes but upstream thinks it doesn't mutate (so it would be skipped on replay), fuzzy lookup is disabled.
- A failed or interrupted execution, or an output starting with `Failed to execute`, disables fuzzy lookup.
- `CacheScope(initial_state_id, policy_version)` keeps entries from different starting databases or canonicalizer versions apart.

**How to explain it:** "We don't replace TVCache; we put a second lookup behind it. Exact first, then our key, then execute. Upstream still does all the sandbox work."

## 6. SQL canonicalizer (Kevin)

`src/fuzzytv/sql/__init__.py`, `SqlCanonicalizer`. It parses one SQLite statement with **SQLGlot** and reports its canonical text and the tables it reads and writes.

**Canonical text.** Generated from the SQLGlot syntax tree, so whitespace, keyword case, comments and operator spacing don't matter. Table aliases are renamed by position (`users u` and `users AS x` both become `users AS _t0`) when that's unambiguous. String literals keep their case (`'Ab'` ≠ `'ab'`).

**Reads and writes.** Every table in the statement is a read (joins, subqueries, CTEs resolved). `INSERT`, `UPDATE`, `DELETE` also write their target table. `CREATE`/`DROP`/`ALTER TABLE` write it (schema counts as table state). `CREATE TABLE … AS SELECT` reads its source query.

**Fail-closed cases** (return unknown or ineligible):

- Parse failure, or more than one statement.
- Volatile functions: `now`, `random()`, `CURRENT_TIMESTAMP`, `date()` with no argument (SQLite reads the clock), `changes()`, `last_insert_rowid()`. These are reads of unknown state.
- Writes nested inside other statements, `SELECT INTO`, temporary or virtual tables, views, triggers.
- System catalogs (`sqlite_master`, `sqlite_sequence`): they reflect every table, and we have no state key for that yet.
- Constructs SQLGlot would *rewrite* rather than reformat (`CAST` types, `GREATEST`/`LEAST`) keep their original text, so they still work but only match exact spellings.

**Midpoint fixes found by the evaluation** (Rohan, on top of Kevin's code):

1. **Column labels.** SQLite names an unaliased result column after its source text exactly as typed: `SELECT SUM(x)` returns a column called `SUM(x)`, `select sum(x)` one called `sum(x)`. If the tool output shows headers, these are different results. Lowercasing them was a correctness bug (two of Kevin's contract tests failed for this reason). Fix: `_labels` recovers each unaliased expression's text from the token stream and appends it to the canonical form as a comment, so differently-typed labels never collide. Aliased columns (`SUM(x) AS total`) and plain columns are unaffected. It also handles labels reaching the output through `*` from a CTE or FROM-subquery.
2. **`PRAGMA table_info(t)`.** LLM agents use it constantly to inspect schemas. It was parsed as an unsupported statement, i.e. *unknown writes*, which permanently disabled fuzzy lookup for the rest of the rollout. Fix: `table_info`, `table_xinfo`, `index_list`, `foreign_key_list` on a named table are now a read of that table. On the SQL workload fuzzy hits went from 4 to 184.

**Testing.** Unit tests pin canonical forms and read/write sets. `tests/unit/test_sql_properties.py` is a randomized differential test: it generates queries and spelling variants, runs them on real SQLite, and checks that any two queries with the same canonical text return the same result (including column labels). It passes at 20,000 queries.

**How to explain it:** "Two queries get the same canonical text only if SQLite would return the same output for them, labels included. Each query also reports exactly which tables it touches."

## 7. Filesystem canonicalizer (Etienne)

`src/fuzzytv/fs/__init__.py`, `FsCanonicalizer`. It handles shell commands for terminal-style agents: `cat`, `ls`, `echo` and `cd`.

**Rule 1: only literal simple commands.** Before even looking at the command name, the tokenizer rejects anything bash could expand, chain or redirect in ways the module doesn't model: `;`, `&`, `|`, `<`, parentheses, `$`, backticks, globs (`*?[`), `~`, `#`, `!`, backslashes, brace expansion, control characters. The only redirect allowed is a trailing `echo … > file` or `>> file`. This is why `ls>out` or `cat a; rm a` can never look like a read.

**Rule 2: resolve paths against the working directory.** The shell session persists between calls, so `cd` changes what `cat a` means. `CallContext.cwd` gives the logical cwd. `./a`, `a` and `/app/a` all resolve to `/app/a` when cwd is `/app`. `..` is collapsed only at the start (because `link/../f` isn't always `f` with symlinks). `/dev`, `/proc`, `/sys` are rejected as volatile.

**What each command reads and writes:**

| Command | Canonical form | Reads | Writes |
| --- | --- | --- | --- |
| `cat a b` | `cat /app/a /app/b` | each file | — |
| `ls d` (known directory) | `ls /app/d` | the directory's entry list | — |
| `ls x` (might be a file) | `ls /app/x # as x` | directory and file | — |
| `echo hi` | `echo hi` | — | — |
| `echo hi > f` | `echo hi > /app/f` | — | the file and its parent directory's entry list |
| `cd d` | `cd /app/d` | — | `fs:cwd` |

Subtleties handled: `ls` on a file prints the path *as typed*, so the typed spelling stays in the key; `cat` with no operand reads stdin (rejected); flags are not supported yet. `grep`, `head`, `tail`, `wc` are recognized as read-only but not analyzed, so they don't disable later fuzzy lookup.

**Testing.** `tests/unit/test_fs_properties.py` runs random command sequences in a temp directory under real bash and checks that equal canonical forms produce equal outputs, and that the tracker's keys change exactly when outputs can.

**How to explain it:** "We only accept commands we can prove are one literal command; we turn every path into an absolute path using the tracked working directory; then `cat` reads a file, `echo >` writes one, `cd` writes the cwd."

## 8. Evaluation harness (Rohan)

`src/fuzzytv/eval/`, run as `python -m fuzzytv.eval run <traces> --modes …`. It replays recorded or generated rollouts through each cache mode and checks every reused value.

**Cache modes compared.**

| Mode | What it does |
| --- | --- |
| `none` | No cache; every call executes. Baseline for work saved. |
| `exact` | Unmodified upstream TVCache. |
| `canonical:<c>` | TVCache with canonical text as keys; full history still must match. Measures what canonicalization alone buys. |
| `relevant_state:<c>` | Our full method: exact first, then the relevant-state fuzzy key. |

**Canonicalizers `<c>`** (`eval/canonicalizers.py`):

- `oracle`: the analysis the generator knows by construction. A perfect canonicalizer, so it is the upper bound.
- `real`: Kevin's and Etienne's code, routed by tool.
- `loose` / `real-loose`: knows only read vs. write and ignores what a read depends on. Unsafe on purpose: it shows what happens without read tracking and proves the validator catches wrong hits.

**Controlled workloads** (`eval/workloads/sql-small.json`, `fs-small.json`; 1,297 calls). Seeded generators build tasks with several rollouts each, mixing: identical prefixes (exact hits possible), spelling variants (canonical hits), inserted writes irrelevant to later reads (relevant-state hits), and relevant writes plus unknown commands (must NOT hit).

**Validation.** For every sampled rollout (100% by default), a separate reference sandbox replays that rollout's own calls directly. A *false hit* is any cached value that differs from the reference value. We report false hits with a 95% Wilson confidence interval. This is why we can say "0 in 681": each of those 681 reused values was compared against real execution.

**Read-only hints** (`--read-only-hints on/off`). TVCache treats every call as mutating unless told otherwise. With hints on, calls the oracle knows are read-only report `will_mutate_state() = False`, as a tool that declares its reads would. This changes the exact baseline (fewer calls in the history) and how much work hits save.

**Hit rate vs. work saved.** Hit rate counts calls served from cache. Executions count calls actually run, including replay. Without hints, a skipped read is still in the history TVCache must replay on the next miss, so hits save little. The harness reports both.

**Backends.** An in-memory model of the TVCache server for fast runs, and the real Rust server for confirmation. `tests/integration/test_eval_backend_parity.py` checks both make identical decisions. On Perlmutter (job 60891246) the real server reproduced every hit, fuzzy-hit and false-hit count; executions differed by at most 4 because of snapshot timing.

**How to explain it:** "We replay the same rollouts through each mode, and every time the cache returns something we check it against actually running the call. The oracle tells us the best possible result; the loose mode shows what goes wrong without read tracking."

## 9. Results, figure by figure

Our canonicalizers are safe (0 wrong hits in 681) and reach about 90% of the oracle's fuzzy hits; with read-only hints they cut SQL executions 42% vs. TVCache's 17%. Numbers below match the poster and `docs/evaluation.md`.

**Figure 2 – share of calls served from cache** (hit rate):

| Workload | Hints | exact (TVCache) | ours (`relevant_state:real`) | oracle |
| --- | --- | --- | --- | --- |
| SQL | off | 5.9% | 40.8% | 45.2% |
| SQL | on | 20.1% | 44.8% | 47.8% |
| Shell | off | 2.2% | 26.4% | 28.7% |
| Shell | on | 11.0% | 28.8% | 31.2% |

What to say: canonicalization alone (`canonical:real`) only lifts SQL from 5.9% to 7.0%; almost all the gain comes from ignoring irrelevant history. Our real canonicalizers get 184 of the oracle's 207 SQL fuzzy hits and 186 of 205 shell ones. The SQL gap is `sqlite_master` queries (27 misses), which have no state key yet; the shell gap is `&&` chains and brace expansion, rejected by design.

**Figure 3 – fuzzy hits that returned a wrong value:**

| Workload | Hints | ours | loose (no read tracking) |
| --- | --- | --- | --- |
| SQL | off | 0 / 184 | 45 / 256 (17.6%) |
| SQL | on | 0 / 151 | 46 / 231 (19.9%) |
| Shell | off | 0 / 186 | 87 / 386 (22.5%) |
| Shell | on | 0 / 160 | 87 / 364 (23.9%) |

What to say: same canonicalizers, but drop the read dependencies and 18–24% of fuzzy hits are wrong. Read tracking is what makes fuzzy reuse safe. Pooled, 0 in 681 gives a 95% upper bound of 0.6%.

**Figure 4 – executions saved vs. no cache, hints on:** exact 17% (SQL) and 9% (shell); ours 42% and 27%; oracle 45% and 29%. With hints off, ours saves only 8% (SQL) and 1% (shell), because skipped reads are replayed later.

**Analysis overhead:** about 1 ms per SQL call (SQLGlot parsing) and about 50 µs per shell call.

**Bugs found by validation (not on the poster; mention if asked):** the column-label bug made both the original oracle and the SQL canonicalizer return wrong hits until fixed; the PRAGMA bug cut fuzzy hits to 4. Both are described in section 6. These show the validator is doing its job: it found real correctness bugs in code that already passed unit tests.

## 10. Real workloads

We use the TVCache paper's two workloads. SkyRL-SQL rollouts are collected; terminal-bench is blocked on a Docker host.

**SkyRL-SQL** (`src/fuzzytv/eval/skyrl_sql.py`, `slurm/collect_skyrl_sql.sbatch`):

- Data: 653 text-to-SQL questions (NovaSky-AI/SkyRL-SQL-653) over the OmniSQL SQLite databases.
- Policy: Qwen2.5-Coder-7B-Instruct served with vLLM on Perlmutter A100s, one shard per GPU, 4 GPUs per node.
- The collector reproduces SkyRL's environment: the model writes `<sql>…</sql>`, gets the result as an observation, and ends with `<solution>…</solution>`, up to a turn limit. Reward is scored on the model's response only (an early bug scored the prompt's example solution).
- Traces are merged and replayed through the harness with `none`, `exact`, `canonical:real` and `relevant_state:real`, every rollout validated.

**Pilot run** (job 60910257; 20 questions × 5 samples; 100 rollouts, 334 SQL calls):

| Mode | Hit rate | Fuzzy hits (wrong) | Executions vs. none |
| --- | --- | --- | --- |
| exact (TVCache) | 33.2% | — | 0.67 |
| canonical:real | 34.4% | 0 | 0.66 |
| relevant_state:real | 34.4% | 5 (0) | 0.66 |

What to say: exact TVCache's 33.2% matches the paper's 33%, which validates our setup. SkyRL-SQL queries are all reads (no INSERT/UPDATE), so relevant-state tracking has nothing to skip; gains here can only come from spelling. The full run (653 questions × 5 samples × 3 passes, job 60910831) is collected; its replay is being rerun after a fix for rollouts with zero tool calls.

Two details from SkyRL's code that matter for caching: observations print a Python `frozenset` of rows, whose order can differ between processes (replay compares sorted rows); and every observation ends with "You have N turns left", which must not be part of a cached value.

**Coverage tool** (`python -m fuzzytv.eval.coverage`): runs our canonicalizers over each benchmark's reference calls, no model needed. 96.6% of SkyRL-SQL gold queries are fuzzy-eligible reads (the rest use `'now'` or `random()`). Only 15.2% of terminal-bench reference solution lines are: most are package installs, `$VAR` expansion and `&&` chains, which fall back to exact.

**terminal-bench** (advisor's container-manager, easy 51 and medium 95 tasks): needs Docker Compose, which Perlmutter compute nodes lack. Also, its server returns the whole tmux pane as output, so results aren't call-local; fuzzy reuse needs per-command output extraction first.

## 11. Infrastructure: repo, tests, Perlmutter

**Repo layout** ([github.com/Harshaan-Chugh/fuzzy-tvcache](https://github.com/Harshaan-Chugh/fuzzy-tvcache)):

| Path | Contents |
| --- | --- |
| `upstream/tvcache-rust`, `upstream/tvclient` | TVCache server and client, as git submodules; never edited in place |
| `src/fuzzytv/` | Our Python package: `interfaces.py`, `state/`, `cache/`, `sql/`, `fs/`, `eval/` |
| `tests/unit/`, `tests/integration/` | Fast deterministic tests; tests that need a running TVCache server |
| `eval/workloads/` | Generated controlled traces |
| `slurm/` | Perlmutter job scripts |
| `docs/` | architecture, interfaces, state tracking, taxonomy, evaluation, roadmap |
| `midpoint/` | poster, midpoint report, this guide |

**Tests:** `pytest -q` runs 488 unit tests (plus skipped ones that need bash or a server). Property tests run SQL against real SQLite and shell commands against real bash. Integration tests start the Rust server and check memory/server parity.

**Perlmutter jobs:**

- `slurm/eval.sbatch`: builds the Rust server on a CPU node and runs the full evaluation against it.
- `slurm/setup_skyrl_sql.sh`: one-time download of data, databases, model and a Python 3.12 environment on scratch.
- `slurm/collect_skyrl_sql.sbatch`: one GPU node, 4 vLLM shards, merge, reward stats, replay.

Practical lessons: compute nodes can't lock files on `$HOME`, so caches live on node-local `/tmp`; imports and model loading from `$HOME` are slow, so the environment and model live on `$SCRATCH`.

## 12. Questions to expect, with answers

**Why not just compare results, or hash the database?** Hashing live state means reading it, which costs about as much as running the call, and needs access to the sandbox. We compute versions from the history of writes alone, before executing anything.

**How do you know a fuzzy hit is correct?** The tracker only reuses a value when the canonical call and the version of every piece of state it reads match. In evaluation we check every reused value against real execution: 0 wrong in 681.

**Isn't 0 in 681 just luck?** The 95% upper bound is 0.6%. And the loose variant, on the same workloads, gets 18–24% wrong, so the workloads clearly contain cases where a careless method fails.

**What if the canonicalizer is wrong?** That is the main risk, so canonicalizers fail closed: anything they can't prove goes to exact TVCache. The SQL one is tested against real SQLite on 20,000 random queries; the shell one against real bash.

**Why are hit rates higher than work saved?** TVCache must replay the history of every call it believes mutates state. Without read-only hints, skipped reads are replayed on the next miss. With hints, our method saves 42% of SQL executions vs. 17% for TVCache.

**Why so little gain on SkyRL-SQL?** Its queries never write, so there is no irrelevant history to skip; only spelling helps (33.2% → 34.4%). Our method is aimed at partially stateful tools, where writes and reads interleave, like terminal tasks and SQL with updates.

**What is the oracle?** The generator builds every call from a structured intent, so it knows the correct analysis without parsing. It is a perfect canonicalizer and gives the upper bound.

**Does exact mode change TVCache?** No. It's the unmodified upstream executor; fuzzy lookup only runs after an exact miss, and the index is stored in the same server under a reserved namespace.

**What about time, randomness, `now()`?** Treated as unknown reads, so never fuzzy-reused.

**Why only `cat`, `ls`, `echo`, `cd`?** They are enough to show the method end to end; widening coverage (`mkdir`, `&&` chains of known commands, known `$VARS`) is in the plan. Everything else still gets exact TVCache.

## 13. Limitations and plan to the final report

**Current limitations:**

- Results on real rollouts are SQL only so far, and SkyRL-SQL is read-only, so it can't show the relevant-state gain.
- Shell coverage is narrow: 15% of terminal-bench reference lines.
- Assumptions documented in the code: no views, triggers or cascading foreign keys in the starting database; no symlinks; `cwd` provided correctly by the environment.
- Terminal-bench output isn't call-local yet.

**Plan:**

| Item | Owner(s) | Notes |
| --- | --- | --- |
| Finish SkyRL-SQL full-run replay and report it | Rohan | Collected; replay rerun pending |
| terminal-bench rollouts on a Linux Docker host | Rohan, advisor | Needs a machine with Docker Compose |
| Call-local terminal output | Harshaan, Etienne | Extract per-command output from the pane |
| Wider shell coverage: `mkdir`, `&&` of known commands, known `$VARS` | Etienne | |
| State key for `sqlite_master` (schema reads) | Kevin, Harshaan | Interface change |
| RL runs with verl (step time, reward) | All | Advisor's setup |
| Upstream PR to TVCache, final figures and report | All | Final week |

## 14. Glossary

| Term | Meaning |
| --- | --- |
| Rollout | One attempt by the agent at a task: a sequence of tool calls and observations |
| Observation | The tool's output shown to the model |
| Mutating call | A call that may change state; TVCache assumes every call is one unless told otherwise |
| Exact hit | TVCache found the same call after the same mutating history |
| Fuzzy hit | Our index found an equivalent call whose read state has the same versions |
| Canonical form | Text that is identical for equivalent calls |
| State key | Name of one piece of state, e.g. `sql:table:main.users`, `fs:file:/app/a` |
| Version | Hash of a piece of state's write history in this rollout |
| Fail closed | When unsure, report unknown and fall back to exact TVCache |
| Oracle | Perfect canonicalizer known from the generator; the upper bound |
| Loose | Deliberately unsafe variant without read tracking |
| False hit | A reused value that differs from real execution |
| Read-only hint | The tool declares a call doesn't mutate, so it is left out of TVCache's history |
| Call-local result | Output depends only on this call and current state, not on earlier output |
| Wilson interval | Confidence interval for a proportion that behaves well at 0 |
