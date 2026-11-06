"""Randomized differential tests of FsCanonicalizer against real bash in a temp tree.

Scale up with FUZZYTV_FS_FUZZ=<n> (default 40 rollouts, 200 fuzzed commands).
Random commands come from a fixed vocabulary with no destructive verbs, and only
ever run with their working directory inside the temp tree.
"""

import os
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from fuzzytv.eval.calls import bash_call
from fuzzytv.fs import FsCanonicalizer
from fuzzytv.interfaces import CWD, CallAnalysis, CallContext, fs_dir, fs_file
from fuzzytv.state.tracker import StateTracker

N = int(os.environ.get("FUZZYTV_FS_FUZZ", "0"))
ROLLOUTS = N or 40
FUZZED = N or 200
FS = FsCanonicalizer()
FILES = {"a.txt": "alpha\n", "b.txt": "beta\n", "d/c.txt": "gamma\n", "d/e/f.txt": "phi\n"}


@pytest.fixture(scope="module", autouse=True)
def require_bash() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return Path(os.path.realpath(tmp_path_factory.mktemp("fs-fuzz")))


def reset(root: Path) -> Path:
    """Recreate the tree at one fixed path, so absolute paths mean the same across rollouts."""
    app = root / "app"
    if app.exists():
        shutil.rmtree(app)
    for name, text in FILES.items():
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        (app / name).write_text(text)
    return app


def bash(command: str, cwd: str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        env={"PATH": os.environ["PATH"], "LC_ALL": "C", "HOME": str(root)},
    )


def spell(rng: random.Random, path: str, cwd: str) -> str:
    rel = os.path.relpath(path, cwd)
    options = [rel, path]
    if not rel.startswith(".."):
        options.append("./" + rel)
    if "/" in rel:
        options.append(rel.replace("/", "//"))
    return rng.choice(options)


def random_step(rng: random.Random, app: Path, cwd: str) -> str:
    # Includes paths that do not exist yet, so reads see both outcomes.
    file = str(app / rng.choice(["a.txt", "b.txt", "d/c.txt", "new.txt", "d/new.txt", "d", "e.txt"]))
    directory = str(app / rng.choice([".", "d", "d/e", "new.txt", "missing"]))
    kind = rng.choices(["cat", "ls", "echo", "append", "cd"], [5, 4, 2, 1, 1])[0]
    text = rng.choice(["hi", "bye", "two words", "'quoted  text'"])
    if kind == "cat":
        return f"cat {spell(rng, file, cwd)}"
    if kind == "ls":
        return f"ls {spell(rng, directory, cwd)}"
    if kind == "cd":
        return f"cd {spell(rng, str(app / rng.choice(['.', 'd', 'd/e'])), cwd)}"
    operator = ">" if kind == "echo" else ">>"
    return f"echo {text} {operator} {spell(rng, file, cwd)}"


def observable(command: str, result: subprocess.CompletedProcess[str]) -> tuple[Any, ...]:
    """What a cached read must reproduce. Failed `cat` errors name the path as typed,
    which is a documented limitation (see docs/state-tracking.md), so stderr is
    compared for failed `cat` only by exit status."""
    if command.startswith("cat") and result.returncode != 0:
        return (result.returncode, result.stdout)
    return (result.returncode, result.stdout, result.stderr)


def run_rollouts(root: Path, seed: int, drop_reads: bool = False) -> tuple[int, list[str]]:
    """Replay random rollouts; return (cache hits, mismatches) over a shared fuzzy cache."""
    rng = random.Random(seed)
    cache: dict[str, tuple[str, tuple[Any, ...]]] = {}
    hits = 0
    mismatches: list[str] = []
    for _ in range(ROLLOUTS):
        app = reset(root)
        cwd = str(app)
        tracker = StateTracker("task")
        for _ in range(10):
            command = random_step(rng, app, cwd)
            analysis = FS.analyze(bash_call(command), CallContext(cwd=cwd))
            if drop_reads and analysis.fuzzy_eligible:
                analysis = CallAnalysis(analysis.canonical)
            key = tracker.fuzzy_key(analysis)
            if command.startswith("cd "):
                moved = bash(f"{command} && pwd", cwd, root)
                if moved.returncode == 0:
                    cwd = moved.stdout.strip()
            else:
                value = observable(command, bash(command, cwd, root))
                if key is not None:
                    earlier = cache.setdefault(key, (command, value))
                    if earlier[1] == value:
                        hits += 1
                    else:
                        mismatches.append(f"{earlier} vs {command!r} -> {value}")
            tracker.observe(analysis)
    return hits, mismatches


def test_same_fuzzy_key_means_same_bash_output(root: Path) -> None:
    hits, mismatches = run_rollouts(root, seed=1)
    assert not mismatches, mismatches[:5]
    assert hits > ROLLOUTS  # not vacuous


def test_differential_harness_detects_missing_read_dependencies(root: Path) -> None:
    # Control: with dependencies dropped, the same comparison must catch false hits.
    _, mismatches = run_rollouts(root, seed=1, drop_reads=True)
    assert mismatches


FUZZ_WORDS = [
    "cat", "ls", "echo", "cd", "grep", "wc", "x", "a.txt", "./a.txt", "d", "d/c.txt", "d/", "..",
    "../up.txt", "out.txt", ">", ">>", ">&", "2>", "&>", ";", "&&", "||", "|", "&", "<", "'", '"',
    "\\", "$X", "$(echo a.txt)", "`echo a.txt`", "*", "?", "[a]", "{a,b}", "{a}", "~", "#", "!",
    "\n", "X=1", "-n", "-l", "''",
]


def fuzz_command(rng: random.Random) -> str:
    words = [rng.choice(FUZZ_WORDS) for _ in range(rng.randint(1, 6))]
    if rng.random() < 0.7:
        words[0] = rng.choice(["cat", "ls", "echo", "cd"])
    return rng.choice(["", " "]).join(words) if rng.random() < 0.3 else " ".join(words)


def snapshot(root: Path) -> dict[str, str | None]:
    """Every path under `root`: file text, or None for a directory."""
    state: dict[str, str | None] = {}
    for path in root.rglob("*"):
        state[str(path)] = path.read_text(errors="replace") if path.is_file() else None
    return state


def changed_paths(before: dict[str, str | None], after: dict[str, str | None]) -> set[str]:
    return {p for p in before.keys() | after.keys() if before.get(p, "<none>") != after.get(p, "<none>")}


def test_reported_writes_cover_what_bash_actually_changes(root: Path) -> None:
    rng = random.Random(2)
    understood = eligible = 0
    for _ in range(FUZZED):
        app = reset(root)
        cwd = str(app / "d")
        command = fuzz_command(rng)
        analysis = FS.analyze(bash_call(command), CallContext(cwd=cwd))
        before = snapshot(root)
        try:
            bash(command, cwd, root)
        except subprocess.TimeoutExpired:
            continue
        changed = changed_paths(before, snapshot(root))
        if analysis.writes_unknown:
            continue
        understood += 1
        eligible += analysis.fuzzy_eligible
        if analysis.fuzzy_eligible or not analysis.writes:
            assert not changed, (command, changed)
        for path in changed:
            assert fs_file(path) in analysis.writes, (command, path, analysis)
            assert fs_dir(os.path.dirname(path)) in analysis.writes, (command, path, analysis)
    assert understood > FUZZED // 20 and eligible > 0  # not vacuous


def test_reported_cwd_matches_bash(root: Path) -> None:
    rng = random.Random(3)
    for _ in range(FUZZED // 3):
        app = reset(root)
        cwd = str(app / "d" / "e")
        target = str(app / rng.choice([".", "d", "d/e"]))
        command = f"cd {spell(rng, target, cwd)}"
        analysis = FS.analyze(bash_call(command), CallContext(cwd=cwd))
        assert analysis.writes == {CWD}
        assert analysis.canonical == f"cd {target}"
        assert bash(f"{command} && pwd", cwd, root).stdout.strip() == target


def test_analyzer_never_raises_and_is_deterministic() -> None:
    rng = random.Random(4)
    alphabet = "cat ls echo cd>|&;'\"\\$`*?[]{},.~#!/\n\t\x00\x7fé\U0001f600-=ab12"
    contexts = [CallContext(), CallContext(cwd="/"), CallContext(cwd="/app"), CallContext(cwd="rel")]
    for _ in range(FUZZED * 10):
        command = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 20)))
        ctx = rng.choice(contexts)
        first = FS.analyze(bash_call(command), ctx)
        assert first == FS.analyze(bash_call(command), ctx)
        assert first.fuzzy_eligible or first.reason or first.writes
