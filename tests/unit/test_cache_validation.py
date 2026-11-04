"""Validation must use the requesting rollout's independent state."""

from types import SimpleNamespace

import pytest

from fuzzytv.cache.validation import ReferenceValidator
from fuzzytv.interfaces import CacheDecision


async def test_detects_incorrect_fuzzy_result(workload: SimpleNamespace) -> None:
    reference = workload.sandbox("task")
    await reference.start()
    validator = ReferenceValidator(reference)
    try:
        read = workload.call("read", path="config.json")
        with pytest.raises(AssertionError, match="result mismatch"):
            await validator.check(
                [read], "incorrect cached value", CacheDecision("fuzzy", "key")
            )
        assert validator.validated_fuzzy_hits == 1
        assert validator.false_fuzzy_hits == 1
        assert not validator.records[0].matched
    finally:
        await reference.stop()


async def test_reference_executes_exact_hit_mutations(
    workload: SimpleNamespace,
) -> None:
    reference = workload.sandbox("task")
    await reference.start()
    validator = ReferenceValidator(reference)
    try:
        write = workload.call("write", path="config.json", text="hello")
        read = workload.call("read", path="config.json")
        await validator.check([write], "OK", CacheDecision("exact"))
        record = await validator.check(
            [write, read], "hello", CacheDecision("fuzzy", "key")
        )
        assert record.matched
        assert len(validator.records) == 2
        assert validator.validated_fuzzy_hits == 1
        assert validator.false_fuzzy_hits == 0
    finally:
        await reference.stop()


async def test_reference_rejects_skipped_history(
    workload: SimpleNamespace,
) -> None:
    reference = workload.sandbox("task")
    await reference.start()
    validator = ReferenceValidator(reference)
    try:
        write = workload.call("write", path="config.json", text="hello")
        read = workload.call("read", path="config.json")
        with pytest.raises(ValueError, match="every logical call"):
            await validator.check(
                [write, read], "hello", CacheDecision("fuzzy", "key")
            )
        assert validator.records == []
    finally:
        await reference.stop()
