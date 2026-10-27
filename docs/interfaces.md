# Shared interfaces (DRAFT — to be designed by Harshaan)

Code location: `src/fuzzytv/interfaces.py` (currently an empty stub).

The split of responsibility:
**Kevin (`sql/`) and Etienne (`fs/`) determine what a call means and which state it reads and
writes. Harshaan (`state/`) decides whether a cached result is reusable.**

## What the contract must express

A canonicalizer takes a tool call (plus context such as cwd and database) and returns something
like an analysis result. It must carry at least:

| Concept | Notes |
|---|---|
| Canonical call identity | A stable string for the normalized call. Equivalent surface forms must map to the same string, and non-equivalent forms must not. |
| Read dependencies | State components the output depends on (e.g. `sql:table:<db>.<t>`, `fs:file:<abs path>`, `fs:dir:<abs path>`). Needs a shared naming scheme. |
| Write dependencies | State components the call mutates. Used to advance state identity. |
| Unknown / unrestricted | A way to say "may read or write anything". This forces exact fallback. |
| Fuzzy-safe flag + reason | Why fuzzy reuse is disallowed (unsupported command, volatile output, parse failure, schema change, transaction, symlink, ...). Rohan logs these. |

## Requirements from upstream (see `architecture.md`)

- The input is a tvclient `ToolCall`. Today it only has `to_dict()`, `serialize()` and
  `will_mutate_state()`. Canonicalizers need tool type and arguments out of `to_dict()`, and the
  dict schema is workload-specific.
- Context beyond the call matters: cwd for fs, current database/schema for SQL. Decide whether
  that context is passed in or tracked by `state/`.
- The output must be pure and deterministic: no I/O, no sandbox access. That makes it unit
  testable and cheap.
- Any failure, exception or unsupported construct must map to "unknown". Never guess.
- Keep it to plain dataclasses or functions unless evidence says otherwise.

## Open questions

- Is the state-key vocabulary strings, or typed tuples?
- Is one call allowed to be both a read and a write (e.g. `UPDATE ... WHERE` reads the table)?
- Who owns the dispatch from tool type to canonicalizer?
- Does Rohan's harness consume the same analysis objects to explain false hits?
