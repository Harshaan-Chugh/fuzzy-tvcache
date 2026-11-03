"""Real-server integration; opt in with --run-tvcache-integration."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from fuzzytv.cache.validation import ReferenceValidator


@pytest.mark.parametrize("backend", ["filesystem", "sqlite"])
async def test_real_server_relevant_state_reuse(
    workload: SimpleNamespace, tvcache_address: tuple[str, int], backend: str,
) -> None:
    host, port = tvcache_address
    task = f"integration-{uuid4().hex}"
    call = workload.call
    results = {}
    validated_fuzzy_hits = 0
    for label in ("A", "B", "C", "D"):
        if backend == "filesystem":
            trace = [
                call("write", path="config.json", text="world" if label == "C" else "hello")
            ]
            if label == "B":
                trace.append(call("write", path="notes.txt", text="changed"))
            read = call("read", path="config.json", style=label)
        else:
            value = "world" if label == "C" else "hello"
            trace = [call("sql_write", sql=f"UPDATE items SET value='{value}'")]
            if label == "B":
                trace.append(call("sql_write", sql="UPDATE notes SET value='changed'"))
            read = call(
                "sql",
                sql=" select value from items " if label == "B" else "SELECT value FROM items",
            )
        if label == "D":
            trace.append(call("unknown"))
        trace.append(read)
        reference = workload.sandbox(task)
        await reference.start()
        validator = ReferenceValidator(reference)
        try:
            async with workload.make_executor(
                task, host=host, port=port, snapshot=True
            ) as executor:
                for index in range(len(trace)):
                    history = trace[: index + 1]
                    returned = await executor.lookup_or_execute(history)
                    await validator.check(history, returned, executor.last_decision)
                results[label] = executor.last_decision.source
                print(
                    f"{backend} rollout {label}: {executor.last_decision.source}; "
                    f"result={returned!r}; independently validated"
                )
                if label == "D":
                    assert executor.tracker.fuzzy_disabled
                    assert executor.last_decision.fuzzy_key is None
            validated_fuzzy_hits += validator.validated_fuzzy_hits
            assert validator.false_fuzzy_hits == 0
        finally:
            await reference.stop()
    assert results == {"A": "execute", "B": "fuzzy", "C": "execute", "D": "execute"}
    assert validated_fuzzy_hits == 1
    assert not workload.live


async def test_real_server_fuzzy_hit_then_snapshot_resume(
    workload: SimpleNamespace, tvcache_address: tuple[str, int],
) -> None:
    host, port = tvcache_address
    task = f"snapshot-integration-{uuid4().hex}"
    call = workload.call
    write = call("write", path="config.json", text="hello")
    seed_read = call("read", path="config.json", mutating=True, style="seed")
    async with workload.make_executor(
        task, host=host, port=port, snapshot=True
    ) as seed:
        await seed.lookup_or_execute([write])
        await seed.lookup_or_execute([write, seed_read])
    read = call("read", path="config.json", mutating=True, style="request")
    notes = call("write", path="notes.txt", text="changed")
    reference = workload.sandbox(task)
    await reference.start()
    validator = ReferenceValidator(reference)
    try:
        async with workload.make_executor(
            task, host=host, port=port, snapshot=True
        ) as executor:
            for history in ([write], [write, read]):
                value = await executor.lookup_or_execute(history)
                await validator.check(history, value, executor.last_decision)
            assert executor.last_decision.source == "fuzzy"
            assert executor.rollout_sandbox is None
            assert executor.sandbox_history == []
            assert executor.total_executions == 0
            before = executor.tracker.versions["fs:file:/app/config.json"]
            history = [write, read, notes]
            value = await executor.lookup_or_execute(history)
            await validator.check(history, value, executor.last_decision)
            # Resume after the cached config write, replay the conservative
            # stateful read, then execute the new notes write.
            assert executor.total_executions == 2
            assert executor.sandbox_history == [item.serialize() for item in history]
            assert executor.tracker.versions["fs:file:/app/config.json"] == before
            assert validator.validated_fuzzy_hits == 1
            assert validator.false_fuzzy_hits == 0
            print("snapshot resume: fuzzy hit left sandbox unchanged; replay validated")
    finally:
        await reference.stop()
