"""Coverage tool: script splitting and classification."""

from pathlib import Path

from fuzzytv.eval.coverage import classify, solution_commands, summarize, terminal_calls
from fuzzytv.interfaces import CallAnalysis, unknown

SCRIPT = """#!/bin/bash
set -euo pipefail
# comment
cd /app
cat a.txt
pip install \\
    numpy
cat > b.py <<'EOF2'
print(1)
EOF2
ls data
"""


def test_solution_commands_fold_continuations_and_heredocs() -> None:
    assert solution_commands(SCRIPT) == [
        "cat a.txt",
        "pip install numpy",
        "cat > b.py <<'EOF2'\nprint(1)\nEOF2",
        "ls data",
    ]


def test_terminal_calls_use_dockerfile_workdir(tmp_path: Path) -> None:
    task = tmp_path / "t1"
    task.mkdir()
    (task / "solution.sh").write_text("ls\n")
    (task / "Dockerfile").write_text("FROM x\nWORKDIR /srv\n")
    assert terminal_calls([tmp_path]) == [("/srv", "ls")]


def test_classify_and_summarize() -> None:
    read = CallAnalysis("cat /a", frozenset({"fs:file:/a"}))
    write = CallAnalysis("echo > /a", frozenset(), frozenset({"fs:file:/a"}))
    volatile = CallAnalysis("date", reads_unknown=True, reason="volatile")
    bad = unknown("glob")
    assert [classify(a) for a in (read, write, volatile, bad)] == [
        "eligible_read", "known_write", "unknown_reads", "unknown_writes",
    ]
    summary = summarize([read, write, volatile, bad])
    assert summary["calls"] == 4 and summary["canonical"] == 0.75
    assert dict(summary["top_reasons"]) == {"volatile": 1, "glob": 1}
