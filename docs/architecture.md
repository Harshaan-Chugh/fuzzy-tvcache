# Architecture: upstream TVCache and where FuzzyTVCache plugs in

Read at upstream commits pinned by the submodules in `upstream/` (`git submodule status`).
Line references are relative to those commits.

## 1. Upstream TVCache today

### Server (`upstream/tvcache-rust`)

The server is a **semantics-free string trie**. It never parses a tool call.

- `TVCache` (`src/tvcache.rs`) = `HashMap<task_id, Arc<Mutex<ToolCallGraph>>>`. **Task id is the
  only isolation boundary.** One TCG per task. `get_tcg` creates it on first use.
- `Node` (`src/tcg/node.rs`): `children: HashMap<String, Node>`, `value: Option<String>`,
  `sandbox_id: Option<String>`, `runtime`, `ttl`, hit counters, `size_in_bytes`.
- `ToolCallGraph::insert(path, value, sandbox_id, ...)` (`src/tcg/tool_call_graph.rs:137`):
  only the **last** path element may be new. If any parent is missing, it returns `(false, false)`
  and stores nothing. The merge rules are: an absent value keeps the existing one, and the first
  sandbox wins.
- `ToolCallGraph::find(path)` (`tool_call_graph.rs:214`) walks the path, returns `value` on an
  exact full-path match, and otherwise returns the **deepest ancestor holding a sandbox**
  (longest-prefix match). The request handler (`src/request_handler.rs:189`) increments that
  sandbox's refcount, so the client must `unref` it (lease).
- Wire protocol: binary, length-prefixed, ops 1–10 (`README.md`, `src/protocol_primitives.rs`).
  `Find` = `(task_id, path: Vec<String>)` → `(value?, sandbox_path?, sandbox_id?)`.
- Eviction: TTL plus memory watermarks run in a background loop (`src/cache_policy_enforcer.rs`,
  `src/eviction/`). Victim sandboxes go to `reclaimable_sandboxes` and are drained only when the
  refcount is 0.
- The upstream README's roadmap item 2 is *"Intelligent lookup to identify equivalent states"*,
  which is this project. That's a good sign for an upstream PR.

### Client (`upstream/tvclient`)

- `ToolCall` (`tvclient/tool_call.py`): abstract `to_dict()`, plus `serialize()` =
  `json.dumps(to_dict(), sort_keys=True)`. The only canonicalization is **dict key order**.
  `will_mutate_state()` **defaults to `True`** (conservative).
- `TvcacheToolExecutor.lookup_or_execute(tool_commands)` (`tvclient/executor.py:101`) is the
  whole lookup policy:
  ```python
  stateful_calls = [c for c in history if c.will_mutate_state()]
  path = [c.serialize() for c in stateful_calls] + [current.serialize()]
  found = await client.find(task_name, path)
  ```
  - Hit: return `found.value`. The live sandbox is *not* touched; it just falls behind
    (`sandbox_history` lags) and gets fast-forwarded on a later miss.
  - Miss: `_acquire_environment` picks the deeper of (forked LPM snapshot, live sandbox), or a
    fresh sandbox. `_execute_and_insert` then replays the stateful suffix plus the current call and
    inserts every prefix value. Snapshots are taken after mutating calls according to
    `SnapshotPolicy`.
  - Values starting with `"Failed to execute"` are never cached (`executor.py:279`).
  - Values are opaque `str`. Whatever the workload's `Sandbox.execute` returns (stdout, stderr,
    exit code, ...) is the "observable response".
- `Sandbox` (`tvclient/sandbox.py`): `start / fork / execute / stop`. `fork()` returns a cold
  immutable snapshot handle.

### Terminal workload backend (`bhaskar792/container-manager-public`)

Read at commit `51a90bb` (not a submodule; Apache-2.0). It is a terminal-bench fork plus a Flask
HTTP server, `rlhf_server/server.py`, that runs Docker containers for RL frameworks such as verl.

- **It does not use tvclient.** It has no `ToolCall`/`Sandbox` subclass and no `to_dict()`. The
  glue that wraps this server for TVCache lives elsewhere (presumably Bhaskar's verl integration)
  and has not been inspected yet.
- HTTP API: `/start` (`task_name`, `rollout_id`), `/execute` (`id`, `command`, `blocking`,
  `timeout`) at `server.py:359`, `/get_pane_logs` (`id`) at `server.py:449`, `/fork`,
  `/fork_async`, `/test`, `/solve`, `/stop`.
- A terminal tool call is therefore most likely a **raw bash string** (`command`). The exact
  `to_dict()` schema is unconfirmed.

Implications for our design:

1. **The shell session persists.** Commands are typed into a long-lived tmux shell in the
   container, so `cd` in one call changes how later relative paths resolve. **cwd is state**:
   filesystem canonicalization needs it as context, and the state tracker must treat `cd` as a
   write to it. The server already has `get_curr_dir_from_tmux_session` (`server.py:289`).
2. **Output may include the whole history.** `/get_pane_logs` returns the entire captured tmux
   pane: earlier commands, their outputs and prompts, plus pre-fork logs when caching is enabled.
   If the cached value is the full pane, it embeds the full history and can **never** be reused
   across different histories. Fuzzy reuse requires the workload's `Sandbox.execute` to return
   only the current command's output. **This is unverified and is the largest open risk.**
3. **Multi-line commands are joined with `&&`** (`server.py:382`), and one `command` may be an
   arbitrary script. Expect most calls to be "unknown" to the filesystem canonicalizer.
   Only simple `cat`/`ls` calls are candidates.
4. `/hash` (`server.py:597`) is a stub that returns `"1111"`. It looks like a planned state-hash
   endpoint, which is relevant to state identity (`state-tracking.md`).

Questions for Bhaskar:

- Where is the `ToolCall`/`Sandbox` glue for terminal-bench and for SkyRL-SQL?
- Is the cached value the full pane log, or only the current command's output?
- What does a SQL tool call look like, and does any SQL workload include writes? The paper's
  SkyRL-SQL workload is read-only (§4.2).
- Where do the containers run relative to Perlmutter GPU nodes? Docker is generally not available
  on NERSC compute nodes.

## 2. What exact matching actually misses

Read-only calls do **not** break matching *if* the workload marks them
`will_mutate_state() == False`. Those calls are dropped from the history key. The real gaps are:

1. **Surface-form differences.** `SELECT name FROM users WHERE id = 1` and
   `select name\nfrom users\nwhere id=1` produce different `serialize()` strings, so they become
   different trie edges. Even with identical history, this is a miss.
2. **Irrelevant mutations in the key.** The key for the current call is the *whole* stateful
   chain. After `UPDATE employees SET salary=100000 WHERE id=1` vs `...=120000...`, a
   `SELECT * FROM departments` misses, even though `departments` is untouched.
   The filesystem case is the same: after `echo hello > unrelated.txt` vs `echo world > unrelated.txt`,
   `cat config.json` misses.
3. **Conservative default classification.** Terminal workloads usually leave
   `will_mutate_state()` at `True` because arbitrary bash can mutate state. Then a `cat` or `ls`
   *extends the chain*, and every later key depends on which reads the agent happened to do and
   in what order. (Confirm per workload which `ToolCall` subclass is used and how it classifies
   calls. container-manager does not define one; see above.)
4. **Mutation order.** Two independent mutations applied in different orders give different keys,
   even when they commute. This is lower priority and probably out of scope.

TODO(Harshaan/Rohan): turn 1–3 into concrete traces under `eval/workloads/` and confirm the miss
against a running server.

## 3. Python integration

Author: Harshaan Chugh.

`RelevantStateExecutor` extends upstream `TvcacheToolExecutor` and delegates its
execution, snapshot, replay, and cleanup operations to upstream methods.
`exact` mode continues to instantiate upstream `TvcacheToolExecutor` directly.

The lookup sequence is:

1. Analyze the original call using the injected canonicalizer and logical context.
2. Compute its pre-call relevant-state key when eligible and explicitly enabled.
3. Perform the existing exact lookup.
4. On an exact miss, try the secondary index if a fuzzy key is available.
5. On a fuzzy miss or unavailable secondary index, continue upstream execution.
6. Observe the completed logical call once.
7. Populate eligible successful results from exact hits or execution.

Original calls, the serialized stateful chain, and sandbox history remain
unchanged. Canonical strings are never executed. Reads conservatively marked
stateful by the workload retain upstream history and replay behavior.

The lookup adapter holds the exact result's sandbox lease while checking the
secondary index. A secondary miss returns that original result to upstream.
A secondary hit releases the exact lease and supplies a value-only result.
A fuzzy hit therefore leaves the requesting live sandbox unchanged.

The secondary index uses a separate client connection and a reserved task
namespace derived from task ID, initial-state identity, and policy version.
Its one-element paths contain fuzzy keys. Entries contain a versioned value
envelope and never contain sandbox snapshots. Secondary runtime metadata is
zero: it is not an execution-time measurement.

All clients sharing the server must reserve the `__fuzzytv_relevant_v1__:`
task prefix for this index. Ordinary task IDs must already isolate initial
state for upstream exact behavior; secondary scoping cannot repair an
incorrect upstream task boundary.

Physical execution monitoring covers both replay and the current call.
An execution exception, cancellation, or upstream failure result disables
fuzzy reuse. Replay never advances logical lineage a second time.

Canonicalizers and context are injected; this layer does not infer workload
schemas or cwd. Terminal reuse remains opt-in until call-local result
semantics are established. Controlled tests supply explicit fixture analyses
and use actual temporary files and SQLite databases.
