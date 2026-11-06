"""The in-memory backend must make the same cache decisions as the real server.

Opt in with --run-tvcache-integration (dedicated server). If this passes,
hit/miss/false-hit numbers from `--backend memory` runs carry over to the
real server; only timings differ.
"""

import pytest

from fuzzytv.eval.runner import Mode, RunConfig, run_mode
from fuzzytv.eval.workloads import generate_fs, generate_sql


@pytest.mark.parametrize("kind", ["sql", "fs"])
@pytest.mark.parametrize("snapshot", ["always", "never"])
@pytest.mark.parametrize("hints", [False, True])
async def test_memory_backend_matches_server(
    tvcache_address: tuple[str, int], kind: str, snapshot: str, hints: bool
) -> None:
    host, port = tvcache_address
    generate = generate_sql if kind == "sql" else generate_fs
    workload = generate(seed=11, tasks=2, rollouts=6)
    for name in (
        "exact", "canonical:oracle", "relevant_state:oracle",
        "canonical:real", "relevant_state:real",
    ):
        decisions = {}
        for backend in ("memory", "server"):
            # Fixed snapshot policy and no simulated latency: decisions must
            # not depend on timing. Runs use fresh task IDs on the server.
            config = RunConfig(
                backend=backend, host=host, port=port, snapshot=snapshot,
                latency_scale=0.0, read_only_hints=hints,
            )
            result = await run_mode(workload, Mode.parse(name), config)
            decisions[backend] = [(r.source, r.executions, r.matched) for r in result.records]
            assert not result.counters.live
        assert decisions["memory"] == decisions["server"], name
        print(f"{kind} {name} snapshot={snapshot} hints={hints}: "
              f"{len(decisions['server'])} identical decisions")
