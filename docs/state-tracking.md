# Relevant-state tracking (DRAFT — design owner: Harshaan)

Goal: a fuzzy key = (isolation boundary, canonical call, identity of the *relevant* state). Two
lookups may share a cached value only if the relevant state is provably the same.

## Hard requirements

1. **No counters as identity.** "Table `t` version 1" is unsafe. Two rollouts that each update
   `t` once get the same version number but different contents. State identity must be derived
   from *what happened* (mutation ancestry or content), not *how many times*.
2. **Isolation.** The key includes task id, and anything else that makes initial state differ
   (dataset/env image, database file, seed). Never share across tasks.
3. **Unknown writes poison state.** A call whose writes are unknown must invalidate everything,
   or make all later lookups in the rollout exact-only.
4. **Fail closed.** If the identity cannot be computed, fall back to exact.

## Candidate approaches (to evaluate, not yet chosen)

- **Lineage hash per component.** `id(c) = H(id_prev(c), canonical_mutation)` for every call
  that writes `c`, starting from `H(task_id, c)`. Equal ids mean the same sequence of canonical
  mutations to `c`. Cost: no live reads. Assumption: a mutation's effect on `c` depends only on
  `c`'s prior state and the mutation text. This is **false** for cross-table writes like
  `INSERT INTO a SELECT FROM b`, for time and randomness, and for fs ops that read other files.
  Such writes must fold in their read dependencies' ids, or be treated as unknown.
- **Content fingerprint.** Hash the actual table rows or file bytes in the sandbox. This proves
  equality but needs a live sandbox at lookup time, which a cache hit is trying to avoid.
  Possibly useful for validation only.
- **Hybrid.** Lineage for the key, fingerprints in Rohan's validation to measure false hits.

## What we will document per approach

- What is **proven** equal vs. **assumed** equal.
- Hazards: SQL schema changes, transactions, triggers/views, cwd changes, symlinks, directory
  listings (affected by creating or deleting any entry), metadata flags (`ls -l` shows mtime).
