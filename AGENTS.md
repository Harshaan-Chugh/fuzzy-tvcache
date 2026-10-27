# Agent instructions (Codex, Claude Code, Cursor, ...)

`CLAUDE.md` is a symlink to this file. Edit this one.

## Project

A fuzzy lookup layer on top of TVCache. Read `docs/architecture.md` before changing anything
that touches cache lookup.

## Rules

- **Correctness over hit rate.** If you're unsure whether reuse is safe, fall back to exact.
  Never introduce a fuzzy hit you can't justify in `docs/state-tracking.md`.
- `exact` mode must stay byte-for-byte upstream behavior.
- Don't edit `upstream/*` casually. Those are submodules (separate repos, unlicensed upstream
  code). Changes there go on a branch in the submodule and get pushed to our fork. Never copy
  upstream source into `src/`.
- Canonicalizers (`src/fuzzytv/sql`, `src/fuzzytv/fs`) are pure functions: no I/O, no sandbox
  access. Unsupported or unparseable input → "unknown", never a guess.
- Each owner's module (see README) should be changeable without touching the others. Shared
  types live only in `src/fuzzytv/interfaces.py`.

## Environment (Perlmutter)

- `module load python/3.13-26.8.1`, then `source .venv/bin/activate`. System python is 3.6.
- Rust: `module load rust/stable`.
- Login node: editing, git, unit tests, small builds only. Benchmarks, servers under load and RL
  go through `slurm/` on compute nodes.
- Large outputs go to `$SCRATCH/fuzzy-tvcache`, never into the repo. Never commit allocation or
  account names, tokens, or `.env` files.

## Testing

- `pytest -q` runs unit tests. They must be deterministic and must not need a server.
- Integration tests (`tests/integration/`) need a TVCache server. Say so explicitly if you didn't
  run them.
- Don't report tests as passing unless you ran them.

## Style

- Python ≥3.10, type hints, small dataclasses over class hierarchies.
- Small, focused commits with imperative messages.
