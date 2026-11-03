# Relevant-state tracking

Author: Harshaan Chugh.

Implementation: `src/fuzzytv/state/tracker.py` (Option B: per-component lineage).
Create one `StateTracker(task_id)` per rollout. It consumes `CallAnalysis`;
it does not parse commands, inspect a sandbox, hash live contents, or implement
cache lookup policy. Import it from `fuzzytv.state.tracker`.

## Component identities and fuzzy keys

Identities are SHA-256 fingerprints of structured JSON, serialized with sorted
object keys, compact separators, ASCII escaping, and UTF-8 encoding. Initial,
write, and fuzzy-key payloads use distinct versioned namespaces:
`fuzzytv.initial.v1`, `fuzzytv.write.v1`, and `fuzzytv.key.v1`.

An unwritten component has the lazy initial identity:

    initial(c) = H(initial namespace, task ID, component name)

Only explicitly updated identities are stored in `versions`. This is lineage,
not a mutation counter: two different first writes must not receive the same ID.
For an understood mutation, each written component receives:

    version(c) = H(write namespace, previous version(c),
                   canonical mutation, sorted read-component/version pairs)

The previous version carries the task and component identity through its initial
ancestor. Every dependency and previous version comes from the pre-write state.
All new identities are computed before any are installed. Thus copying B into A
includes B's identity, and simultaneous writes do not depend on update order.
The tracker trusts the supplied write set; it does not infer extra dependencies.

An eligible read receives:

    key = H(key namespace, task ID, canonical call,
            sorted read-component/version pairs)

Only the components listed in `reads` contribute. Changing notes.txt does not
change a config.json read key. Changing config.json does. Task ID contributes
both to initial identities and to lookup keys. A fully understood read with no
dependencies is eligible and depends only on task ID and canonical text.

## Unsafe calls and execution order

`fuzzy_key()` returns `None` for ineligible calls or a disabled rollout.
Eligibility comes from `CallAnalysis.fuzzy_eligible`: canonical text, no unknown
reads or writes, and no known writes. `observe()` permanently disables fuzzy
lookup, in this priority order, for:

1. Unknown writes, even when the known write set is empty.
2. Known writes without canonical text.
3. Known writes with unknown read dependencies.

The first disabling reason is retained, including analyzer diagnostics when
available. Disabling leaves existing versions unchanged; later observations are
no-ops and all later fuzzy keys are `None`. A fresh tracker starts enabled.
There is no reset API. Disabling affects only fuzzy reuse; the integration caller
must continue ordinary TVCache lookup/execution.

An unsupported read-only call with unknown reads is itself ineligible but does
not change identities or disable later supported reads. Missing canonical text
without any writes also leaves the rollout enabled.

The caller analyzes a call, computes its key against pre-call state, attempts
lookup/execution, then observes its analysis once the call enters logical rollout
history. Writes must be observed exactly once, including exact hits whose physical
execution may later be materialized by replay. Replaying a sandbox must not observe
the same logical mutations a second time. Failed or partially executed mutations
whose effects cannot be determined must disable fuzzy reuse, for example by
observing `unknown(reason)`; this API does not carry execution outcomes.

`task_id`, `versions`, `fuzzy_disabled`, and `disabled_reason` are exposed for
inspection. Callers must not mutate them. A tracker belongs to one sequential
rollout; it is not a concurrent shared-state service.

## Guarantees and assumptions

Assuming no fingerprint collisions, equal lineage identities establish equal
encoded mutation ancestry and tracked dependency identities. Irrelevant writes
do not affect a read's key; known relevant writes update affected identities.
Unsafe writes disable fuzzy matching.

Real-state equivalence additionally requires:

1. Rollouts sharing a task ID start with equivalent relevant initial state.
   Future integration must include environment, dataset, or seed identity when
   task ID alone is insufficient.
2. Canonicalizers report all read and write dependencies.
3. Equal canonical strings preserve relevant semantics.
4. Eligible reads are deterministic given their tracked state. Time, randomness,
   external services, and other uncontrolled inputs must make calls ineligible.
5. Mutations are determined by each written component's prior state, canonical
   operation, and all reported read dependencies. Other mutations are unsafe.
6. Cached values contain the current call's observable result. Accumulated
   terminal output may depend on unrelated history; real terminal integration
   has not established this precondition.
7. Component naming and invalidation are sound. SQL schema changes, triggers,
   views, transactions, filesystem aliases, symlinks, directory entries, and
   metadata require correct dependency handling or conservative rejection.

Use the shared component helpers. A SQL table key covers rows and schema;
creating/deleting a file also writes its parent directory key. A file read need
not depend on its parent directory's listing. Metadata-dependent commands such
as `ls -l` require more state than the entry-name directory component provides.
Cwd changes write `CWD`; resolving later relative paths still requires a sound
`CallContext.cwd`, which this tracker does not discover or maintain.

Lineage equality is therefore not an unconditional proof of equal live contents.
Different histories can also produce equal contents but different identities,
causing conservative false misses. An overwrite does not erase prior ancestry.
Actual bytes/rows hashing may be useful in future validation but is not part of
this tracker. No dependencies, shared interfaces, canonicalizers, upstream
submodules, or exact cache behavior are changed by this slice.

## Mock demonstration and tests

Run `pytest -q -s tests/unit/test_mock_rollouts.py`, then `pytest -q` for the
complete unit suite. Neither requires a server or running sandbox.

Each mock rollout starts from the same fake world and task ID:

- A writes hello to config, misses, executes, and stores the read result.
- B performs the same config write and an unrelated notes write, then hits A's key.
- C writes world to config, obtains a different key, and executes on a miss.
- D performs an unknown write; both subsequent reads execute without fuzzy lookup.

Every read is independently executed against its requesting rollout's fake world
for validation, including cache hits. Policy executions and validation executions
are counted separately: four policy reads, five validation reads, two stored
entries. B's extra directory write does not enter its file-only read key. D never
accesses or populates the fuzzy index after disabling.

Tracker unit tests cover the fourteen agreed scenarios: identical state/call,
equivalent canonical text, unrelated and relevant writes, different and equivalent
cross-rollout writes, mutation order, unknown writes, unsupported read-only calls,
missing mutation canonical text, unknown mutation reads, cross-component reads,
task isolation, and write ineligibility. Additional checks cover simultaneous
pre-write dependencies, deterministic sorted serialization, key field separation,
and stateless reads. This demonstrates the tracker/cache boundary; it does not
exercise the real TVCache server, replay, or terminal integration.

## Future integration boundary

The candidate integration is a Python wrapper around `TvcacheToolExecutor`:
try existing exact lookup first, then an eligible secondary fuzzy lookup; return
a fuzzy result without changing the requesting sandbox; use normal TVCache on a
miss or unsafe analysis; populate the fuzzy index only for eligible results.
A derived task namespace and one-element path could reuse the Rust trie without
changing its topology or sandbox lifecycle. This policy is not implemented here.

The architecture document's older candidate suggests fuzzy lookup before exact.
The agreed handoff specifies exact first; reconcile that candidate when doing real
integration. The planned evaluation modes remain `none` (execute everything),
`exact` (unmodified TVCache), `canonical` (normalized calls with full stateful
history), and `relevant_state` (canonical call plus relevant-state identity).

Before real integration, establish the actual workload call schema, reliable cwd
context, call-local observable output, initial-state isolation, and observation
ordering through exact hits and replay. Validate reused results in the requesting
rollout's state, including failed mutations and sandbox lifecycle behavior.
