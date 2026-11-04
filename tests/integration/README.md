# Cache integration validation

Author: Harshaan Chugh.

`test_cache_integration.py` uses the real TVCache client, executor, and Rust
server with controlled filesystem and SQLite sandboxes. Analyses are explicit
fixtures; production canonicalizers and terminal backends are not exercised.

Tests cover relevant-state reuse, rejection after relevant or unknown writes,
and snapshot resume after a fuzzy hit. Every logical result is compared with
an independent sandbox executing the original trace without caching.

Use a dedicated server. Tests create temporary sandbox/snapshot handles whose
lifetime is limited to the test run; do not share that server with workloads.
Set `TVCACHE_HOST` and `TVCACHE_PORT` if needed, then run:

```bash
pytest -q -s --run-tvcache-integration tests/integration
```

The default `pytest -q` skips these tests. On Perlmutter, submit
`slurm/cache-integration.sbatch` with allocation/QOS/constraint options supplied
on the command line. It stages the server source and cached Cargo registry on
scratch because compute nodes cannot acquire Cargo locks on the home filesystem.
The build is offline and requires a populated Cargo registry cache.
Build artifacts and logs stay under `$SCRATCH/fuzzy-tvcache`.
