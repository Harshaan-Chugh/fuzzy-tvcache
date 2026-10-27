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
   calls. See the container-manager repo.)
4. **Mutation order.** Two independent mutations applied in different orders give different keys,
   even when they commute. This is lower priority and probably out of scope.

TODO(Harshaan/Rohan): turn 1–3 into concrete traces under `eval/workloads/` and confirm the miss
against a running server.

## 3. Integration points (candidates, not decisions)

Because the server is semantics-free and the key is built in one place
(`executor.py:110-111`), the natural integration is **client-side, in Python**:

- Wrap or subclass `TvcacheToolExecutor` and override `lookup_or_execute`. Before the existing
  exact lookup, try a fuzzy lookup. On a fuzzy miss or an unsafe call, fall through to the
  unchanged exact path.
- **Secondary index without server changes:** the server's isolation unit is an arbitrary
  `task_id` string. A fuzzy index could use a derived namespace (e.g. `task_id + "::fuzzy"`) with
  a single-element path `[fuzzy_key]` and `sandbox_id=None`. That reuses `Insert`/`Find`, TTL and
  eviction as-is, and leaves the TCG topology and sandbox lifecycle untouched. A Rust-side index
  is an option later if a cleaner upstream PR needs one.
- **Fuzzy hit must not change sandbox state.** A fuzzy hit should behave like today's exact hit:
  return the value and leave `rollout_sandbox`/`sandbox_history` alone. If the call itself mutates
  state, skipping its execution would desynchronize the sandbox. So, at least initially, fuzzy
  reuse should apply only to calls classified as non-mutating.
- The exact TCG insert path stays as it is, so `exact` mode is upstream behavior byte-for-byte.

Open questions for the interface design: where the relevant-state identity is computed (client
only?), and how the fuzzy entry gets populated (on every exact insert of an eligible call?).
