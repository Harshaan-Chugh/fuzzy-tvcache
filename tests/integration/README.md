Integration tests that need a running TVCache server (`upstream/tvcache-rust`).
Start the server on a compute node (or locally for tiny tests) and point tests at it
via `TVCACHE_HOST` / `TVCACHE_PORT`. None exist yet.
