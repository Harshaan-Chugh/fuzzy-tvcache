"""Correctness tests for deterministic relevant-state lineage."""

import hashlib
import json

import pytest

from fuzzytv.interfaces import CallAnalysis, fs_file, unknown
from fuzzytv.state.tracker import StateTracker


A = fs_file("/app/config.json")
B = fs_file("/app/notes.txt")
READ = CallAnalysis("cat /app/config.json", reads=frozenset({A}))


def write(component: str, value: str) -> CallAnalysis:
    return CallAnalysis(
        canonical=f"write {component} {value!r}",
        writes=frozenset({component}),
    )


def test_identical_call_and_state() -> None:
    left, right = StateTracker("task"), StateTracker("task")
    assert not left.fuzzy_disabled
    assert left.disabled_reason is None
    assert left.fuzzy_key(READ) is not None
    assert left.fuzzy_key(READ) == right.fuzzy_key(READ)
    left.observe(READ)
    assert left.versions == right.versions == {}


def test_equivalent_calls_with_identical_canonical_text() -> None:
    # Formatting normalization belongs to the canonicalizer, not the tracker.
    normalized = CallAnalysis(READ.canonical, reads=frozenset({A}))
    tracker = StateTracker("task")
    assert tracker.fuzzy_key(READ) == tracker.fuzzy_key(normalized)


def test_unrelated_write_preserves_key() -> None:
    tracker = StateTracker("task")
    before = tracker.fuzzy_key(READ)
    tracker.observe(write(B, "notes"))
    assert set(tracker.versions) == {B}
    assert tracker.fuzzy_key(READ) == before


def test_relevant_write_changes_key() -> None:
    tracker = StateTracker("task")
    before = tracker.fuzzy_key(READ)
    tracker.observe(write(A, "hello"))
    assert set(tracker.versions) == {A}
    assert tracker.fuzzy_key(READ) != before


def test_different_writes_across_rollouts() -> None:
    left, right = StateTracker("task"), StateTracker("task")
    left.observe(write(A, "hello"))
    right.observe(write(A, "world"))
    assert left.versions[A] != right.versions[A]
    assert left.fuzzy_key(READ) != right.fuzzy_key(READ)


def test_equivalent_writes_across_rollouts() -> None:
    left, right = StateTracker("task"), StateTracker("task")
    left.observe(write(A, "hello"))
    right.observe(write(A, "hello"))
    assert left.versions[A] == right.versions[A]
    assert left.fuzzy_key(READ) == right.fuzzy_key(READ)


def test_different_write_order_changes_lineage() -> None:
    left, right = StateTracker("task"), StateTracker("task")
    # Both histories finish with "final", but ancestry still differs.
    # This intentionally permits a conservative false miss.
    for value in ("hello", "world", "final"):
        left.observe(write(A, value))
    for value in ("world", "hello", "final"):
        right.observe(write(A, value))
    assert left.versions[A] != right.versions[A]
    assert left.fuzzy_key(READ) != right.fuzzy_key(READ)


@pytest.mark.parametrize(
    ("unsafe", "expected_reason"),
    [
        (unknown("unsupported command: python"), "unknown writes"),
        (
            CallAnalysis(None, writes=frozenset({A})),
            "write without canonical text",
        ),
        (
            CallAnalysis("copy mystery", writes=frozenset({A}), reads_unknown=True),
            "write with unknown read dependencies",
        ),
    ],
    ids=["unknown-write", "missing-canonical", "unknown-write-dependencies"],
)
def test_unsafe_write_permanently_disables(
    unsafe: CallAnalysis, expected_reason: str
) -> None:
    tracker = StateTracker("task")
    tracker.observe(write(B, "existing history"))
    before = tracker.versions.copy()
    tracker.observe(unsafe)
    assert tracker.fuzzy_disabled
    assert tracker.disabled_reason is not None
    assert tracker.disabled_reason.startswith(expected_reason)
    if unsafe.reason:
        assert unsafe.reason in tracker.disabled_reason
    first_reason = tracker.disabled_reason
    for subsequent in (READ, write(A, "safe"), unknown("later failure"), READ):
        assert tracker.fuzzy_key(READ) is None
        tracker.observe(subsequent)
        assert tracker.fuzzy_disabled
        assert tracker.disabled_reason == first_reason
        assert tracker.versions == before
    assert StateTracker("task").fuzzy_key(READ) is not None


def test_unknown_read_only_preserves_later_eligibility() -> None:
    tracker = StateTracker("task")
    tracker.observe(write(A, "hello"))
    before = tracker.fuzzy_key(READ)
    versions = tracker.versions.copy()
    unsupported = CallAnalysis(
        None, reads_unknown=True, reason="unsupported command: grep"
    )
    assert tracker.fuzzy_key(unsupported) is None
    tracker.observe(unsupported)
    assert not tracker.fuzzy_disabled
    assert tracker.disabled_reason is None
    assert tracker.versions == versions
    assert tracker.fuzzy_key(READ) == before


def test_cross_component_dependency_changes_destination_lineage() -> None:
    left, right = StateTracker("task"), StateTracker("task")
    left.observe(write(B, "hello"))
    right.observe(write(B, "world"))
    copy = CallAnalysis(
        "copy notes to config", reads=frozenset({B}), writes=frozenset({A})
    )
    left.observe(copy)
    right.observe(copy)
    assert left.versions[A] != right.versions[A]
    assert left.fuzzy_key(READ) != right.fuzzy_key(READ)


def test_task_isolation() -> None:
    left, right = StateTracker("task-a"), StateTracker("task-b")
    assert left.fuzzy_key(READ) != right.fuzzy_key(READ)
    left.observe(write(A, "hello"))
    right.observe(write(A, "hello"))
    assert left.versions[A] != right.versions[A]
    assert left.fuzzy_key(READ) != right.fuzzy_key(READ)


def test_write_is_ineligible() -> None:
    assert StateTracker("task").fuzzy_key(write(A, "hello")) is None


def test_multiple_writes_use_pre_write_dependencies() -> None:
    combined = StateTracker("task")
    dependencies = frozenset({A, B})
    combined.observe(
        CallAnalysis("swap", reads=dependencies, writes=frozenset({A, B}))
    )
    for component in (A, B):
        isolated = StateTracker("task")
        isolated.observe(
            CallAnalysis("swap", reads=dependencies, writes=frozenset({component}))
        )
        assert combined.versions[component] == isolated.versions[component]


def test_key_uses_sorted_dependencies_and_stable_serialization() -> None:
    tracker = StateTracker("task")
    for component in (B, A):
        tracker.observe(write(component, "hello"))
    analysis = CallAnalysis("cat both", reads=frozenset({B, A}))
    # Specify the public key format independently, including dependency order.
    payload = {
        "namespace": "fuzzytv.key.v1",
        "task_id": "task",
        "canonical": "cat both",
        "reads": [(A, tracker.versions[A]), (B, tracker.versions[B])],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert tracker.fuzzy_key(analysis) == hashlib.sha256(encoded).hexdigest()


def test_canonical_call_and_dependency_set_distinguish_keys() -> None:
    tracker = StateTracker("task")
    assert tracker.fuzzy_key(READ) != tracker.fuzzy_key(
        CallAnalysis("different read", reads=READ.reads)
    )
    assert tracker.fuzzy_key(READ) != tracker.fuzzy_key(
        CallAnalysis(READ.canonical, reads=frozenset({A, B}))
    )


def test_stateless_read_remains_eligible_after_known_writes() -> None:
    tracker = StateTracker("task")
    pure = CallAnalysis("SELECT 1")
    before = tracker.fuzzy_key(pure)
    assert before is not None
    tracker.observe(write(A, "hello"))
    assert tracker.fuzzy_key(pure) == before
    tracker.observe(unknown("untracked mutation"))
    assert tracker.fuzzy_key(pure) is None
