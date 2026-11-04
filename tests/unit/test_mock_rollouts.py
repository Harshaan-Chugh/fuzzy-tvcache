"""Run with: pytest -q -s tests/unit/test_mock_rollouts.py."""

import json

from fuzzytv.interfaces import CallAnalysis, fs_dir, fs_file, unknown
from fuzzytv.state.tracker import StateTracker


CONFIG = "/app/config.json"
NOTES = "/app/notes.txt"
READ = CallAnalysis("cat /app/config.json", reads=frozenset({fs_file(CONFIG)}))


def execute_read(world: dict[str, str], path: str) -> str:
    """Inspect the requesting world's actual contents, without tracker state."""
    return world[path]


def execute_write(
    world: dict[str, str], tracker: StateTracker, path: str, value: str
) -> None:
    # Execute from explicit inputs; canonical text is never interpreted.
    world[path] = value
    tracker.observe(
        CallAnalysis(
            canonical=json.dumps(["write", path, value], separators=(",", ":")),
            writes=frozenset({fs_file(path), fs_dir("/app")}),
        )
    )


def test_four_rollouts_with_independent_validation() -> None:
    cache: dict[str, str] = {}
    outcomes: list[tuple[str, str, str]] = []
    keys: dict[str, str | None] = {}
    policy_executions = 0
    validation_executions = 0

    for label in ("A", "B", "C", "D"):
        world = {CONFIG: "initial", NOTES: "initial notes"}
        tracker = StateTracker("shared-task")
        execute_write(world, tracker, CONFIG, "world" if label == "C" else "hello")

        if label == "B":
            execute_write(world, tracker, NOTES, "unrelated mutation")
        if label == "D":
            # Model an arbitrary command changing config without safe analysis.
            world[CONFIG] = "changed by arbitrary script"
            tracker.observe(unknown("unsupported command: python"))

        # D performs multiple subsequent reads to demonstrate lasting disabling.
        for _ in range(2 if label == "D" else 1):
            key = tracker.fuzzy_key(READ)
            keys[label] = key
            before_read = world.copy()
            if key is not None and key in cache:
                status = "HIT"
                result = cache[key]
            else:
                status = "DISABLED" if key is None else "MISS"
                result = execute_read(world, CONFIG)
                policy_executions += 1
                if key is not None:
                    cache[key] = result

            # Always perform a separate validation read, including on a hit.
            actual = execute_read(world, CONFIG)
            validation_executions += 1
            assert result == actual
            assert world == before_read
            tracker.observe(READ)
            outcomes.append((label, status, result))
            print(f"Rollout {label}: {status}; result={result!r}; validated")

    assert keys["A"] is not None
    assert keys["A"] == keys["B"]
    assert keys["C"] is not None and keys["C"] != keys["A"]
    assert keys["D"] is None
    assert outcomes == [
        ("A", "MISS", "hello"),
        ("B", "HIT", "hello"),
        ("C", "MISS", "world"),
        ("D", "DISABLED", "changed by arbitrary script"),
        ("D", "DISABLED", "changed by arbitrary script"),
    ]
    assert len(cache) == 2
    assert policy_executions == 4
    assert validation_executions == 5
