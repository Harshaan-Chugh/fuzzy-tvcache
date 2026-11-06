"""Canonicalizer coverage on real benchmark text (no model, GPU or Docker needed).

    python -m fuzzytv.eval.coverage sql train.parquet       # SkyRL-SQL gold queries
    python -m fuzzytv.eval.coverage terminal tasks_dir ...  # terminal-bench solution.sh lines

For each call it reports whether the real canonicalizer returned a canonical
form and whether the call is fuzzy-eligible (known reads, no unknown reads or
writes), plus the most common reasons for the rest. Gold queries and reference
solutions are a proxy for model-written calls: they show which constructs the
benchmarks use, not how often a policy repeats them.
"""

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fuzzytv.interfaces import CallAnalysis, CallContext

HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
SKIP = re.compile(r"^(#|set\s+-|cd\s+\S+\s*$)")


def classify(analysis: CallAnalysis) -> str:
    if analysis.writes_unknown:
        return "unknown_writes"
    if analysis.canonical is None:
        return "no_canonical"
    if analysis.reads_unknown:
        return "unknown_reads"
    if analysis.writes:
        return "known_write"
    return "eligible_read"


def summarize(analyses: Iterable[CallAnalysis]) -> dict[str, Any]:
    analyses = list(analyses)
    classes = Counter(classify(a) for a in analyses)
    reasons = Counter(a.reason for a in analyses if a.reason)
    total = len(analyses)
    return {
        "calls": total,
        "canonical": sum(a.canonical is not None for a in analyses) / total if total else 0.0,
        "classes": {k: v / total for k, v in sorted(classes.items())},
        "top_reasons": reasons.most_common(8),
    }


def sql_calls(parquet: Path) -> list[str]:
    import pandas as pd

    frame = pd.read_parquet(parquet)
    return [spec["ground_truth"] for spec in frame["reward_spec"]]


def solution_commands(script: str) -> list[str]:
    """Top-level command lines of a shell script; heredoc bodies and
    continuations are folded into the line that starts them."""
    commands, pending, heredoc = [], "", None
    for line in script.splitlines():
        if heredoc is not None:
            pending += "\n" + line
            if line.strip() == heredoc:
                commands.append(pending)
                pending, heredoc = "", None
            continue
        stripped = line.strip()
        if not pending and (not stripped or stripped.startswith("#!") or SKIP.match(stripped)):
            continue
        pending = f"{pending} {stripped}".strip() if pending else stripped
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        match = HEREDOC.search(stripped)
        if match:
            heredoc = match.group(1)
            continue
        commands.append(pending)
        pending = ""
    if pending:
        commands.append(pending)
    return commands


def terminal_calls(task_dirs: list[Path]) -> list[tuple[str, str]]:
    """(cwd, command) for every solution.sh line under the given task directories."""
    calls = []
    for root in task_dirs:
        for solution in sorted(root.glob("*/solution.sh")):
            dockerfile = solution.parent / "Dockerfile"
            workdirs = re.findall(r"^WORKDIR\s+(\S+)", dockerfile.read_text(), re.M) if (
                dockerfile.exists()
            ) else []
            cwd = workdirs[-1] if workdirs else "/app"
            calls += [(cwd, c) for c in solution_commands(solution.read_text())]
    return calls


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m fuzzytv.eval.coverage")
    parser.add_argument("kind", choices=["sql", "terminal"])
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if args.kind == "sql":
        from fuzzytv.sql import SqlCanonicalizer

        canon, ctx = SqlCanonicalizer(), CallContext(database="main")
        analyses = [canon.analyze({"sql": q}, ctx) for p in args.paths for q in sql_calls(p)]
    else:
        from fuzzytv.fs import FsCanonicalizer

        canon = FsCanonicalizer()
        analyses = [
            canon.analyze({"command": c}, CallContext(cwd=cwd))
            for cwd, c in terminal_calls(args.paths)
        ]
    print(json.dumps(summarize(analyses), indent=2))


if __name__ == "__main__":
    main()
