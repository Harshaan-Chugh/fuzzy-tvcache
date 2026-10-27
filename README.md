# FuzzyTVCache

**Fuzzy Cache Lookups for Partially Stateful Tool Calls by Canonicalization.** Cornell, Team 30.
Advisor: Bhaskar Kataria.

[TVCache](https://github.com/abhivijay96/tvcache-rust) caches tool-call results during RL
rollouts. It keys each result on the exact sequence of prior state-mutating calls. FuzzyTVCache
adds a fuzzy lookup layer that does two things:

1. **Canonicalizes** calls, so formatting-only differences (SQL whitespace/case, `./a` vs `a`)
   hit the same entry.
2. **Tracks relevant state**, so a read that doesn't depend on an earlier mutation can reuse a
   cached result across histories that differ only in irrelevant ways.

It falls back to exact TVCache whenever safety can't be established.

Status: **setup only.** No fuzzy matching is implemented yet. See [docs/roadmap.md](docs/roadmap.md).

| Who | Area | Code |
|---|---|---|
| Harshaan Chugh | Shared interfaces, state tracking, cache integration | `src/fuzzytv/interfaces.py`, `src/fuzzytv/state/` |
| Kevin Rodriguez | SQL canonicalization (SQLGlot) | `src/fuzzytv/sql/` |
| Etienne Sasenarine | Filesystem canonicalization (`cat`, `ls`) | `src/fuzzytv/fs/` |
| Rohan Shankar | Baselines, validation, evaluation | `eval/`, `tests/integration/` |

## Layout

```
upstream/tvcache-rust   git submodule: TVCache server (Rust)
upstream/tvclient       git submodule: TVCache Python client
src/fuzzytv/            our code (Python package)
tests/                  unit/ (pure, fast) and integration/ (needs a server)
eval/                   workloads and benchmark scripts
slurm/                  compute-node job scripts
configs/                example configs (local copies are gitignored)
docs/                   architecture, interfaces, state tracking, taxonomy, evaluation, roadmap
```

Start with [docs/architecture.md](docs/architecture.md). It covers how upstream TVCache works and
where we plug in.

## Upstream code and submodules

Our repo stores only a *pointer* (URL plus commit) to each upstream repo. It doesn't store a
copy. The upstream repos currently have **no LICENSE file**, so we don't vendor or copy their
source. We reference it via submodules and contribute changes back by PR.

- Clone everything: `git clone --recurse-submodules <our repo url>`, or after a plain clone run
  `git submodule update --init`.
- To change upstream code: work inside `upstream/<repo>` on a branch, commit, push to **our
  fork**, and open a PR to upstream. Then commit the updated pointer in this repo
  (`git add upstream/<repo>`).
- One-time setup (Harshaan): fork both repos on GitHub, then
  ```bash
  git config -f .gitmodules submodule.upstream/tvcache-rust.url https://github.com/Harshaan-Chugh/tvcache-rust
  git config -f .gitmodules submodule.upstream/tvclient.url https://github.com/Harshaan-Chugh/tvclient
  git submodule sync && git commit -am "Point submodules at team forks"
  ```
  Inside each submodule, `git remote add upstream https://github.com/abhivijay96/<repo>` lets
  you pull upstream updates.

## Setup on Perlmutter

```bash
module load python/3.13-26.8.1      # system python3 is 3.6, too old
python -m venv .venv && source .venv/bin/activate
pip install -e upstream/tvclient -e '.[dev]'
pytest -q                           # smoke tests (login node is fine)
```

Rust server (needs `module load rust/stable`):
`cd upstream/tvcache-rust && cargo build && cargo run`. It listens on `:7878`.

Run anything heavier than unit tests on a compute node:
`sbatch -A <account> -q <qos> -C cpu slurm/smoke.sbatch`. Outputs go to
`$FUZZYTV_OUT` (default `$SCRATCH/fuzzy-tvcache`). Don't commit results, datasets or checkpoints.

## Adding collaborators

On GitHub, go to the repo's **Settings → Collaborators → Add people** and add Rohan, Etienne and
Kevin with *Write* access. Do the same on the two forks if they'll push upstream changes.

## Agent instructions

See [AGENTS.md](AGENTS.md). `CLAUDE.md` is a symlink to it.
