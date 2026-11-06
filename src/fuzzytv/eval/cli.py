"""Command line: `python -m fuzzytv.eval {generate,run,plot} ...`.

    python -m fuzzytv.eval generate --kind sql --seed 0 -o eval/workloads/sql-small.json
    python -m fuzzytv.eval run eval/workloads/sql-small.json eval/workloads/fs-small.json
    python -m fuzzytv.eval plot $FUZZYTV_OUT/eval/<run>

`run` writes to $FUZZYTV_OUT/eval/<name> ($FUZZYTV_OUT defaults to
$SCRATCH/fuzzy-tvcache, else ./results): config.json, summary.json,
summary.csv, calls.jsonl, mismatches.jsonl.
"""

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fuzzytv.eval.runner import (
    DEFAULT_MODES,
    CostModel,
    Mode,
    RunConfig,
    record_rows,
    run_mode,
    summarize,
)
from fuzzytv.eval.workloads import GENERATORS, Perturbation, Workload

CSV_COLUMNS = [
    "workload", "mode", "read_only_hints", "calls", "hit_rate", "exact_hits",
    "fuzzy_hits", "misses", "validated_fuzzy_hits", "false_fuzzy_hits",
    "false_hit_rate", "false_exact_hits", "physical_executions", "sandbox_starts",
    "sandbox_restores", "sandbox_forks", "analysis_us_per_call",
    "modeled_tool_seconds", "policy_seconds", "wall_seconds",
]


def default_out() -> Path:
    if "FUZZYTV_OUT" in os.environ:
        return Path(os.environ["FUZZYTV_OUT"])
    if "SCRATCH" in os.environ:
        return Path(os.environ["SCRATCH"]) / "fuzzy-tvcache"
    return Path("results")


def _generate(args: argparse.Namespace) -> None:
    perturbation = Perturbation(
        p_variant=args.p_variant, p_noise=args.p_noise, p_drop=args.p_drop,
        p_stop=args.p_stop,
    )
    name = args.name or Path(args.output).stem
    workload = GENERATORS[args.kind](
        name=name, seed=args.seed, tasks=args.tasks, rollouts=args.rollouts,
        perturbation=perturbation,
    )
    workload.save(Path(args.output))
    print(f"wrote {args.output}: {len(workload.tasks)} tasks, {workload.total_calls} calls")


async def _run(args: argparse.Namespace) -> Path:
    cost = CostModel(**json.loads(args.cost)) if args.cost else CostModel()
    out = Path(args.out or default_out()) / "eval" / (
        args.name or time.strftime("run-%Y%m%d-%H%M%S")
    )
    out.mkdir(parents=True, exist_ok=True)
    modes = [Mode.parse(m) for m in (args.modes or DEFAULT_MODES)]
    hint_settings = {"off": [False], "on": [True], "both": [False, True]}[args.read_only_hints]
    workloads = [Workload.load(Path(p)) for p in args.workloads]
    (out / "config.json").write_text(json.dumps({
        "argv": sys.argv, "workloads": args.workloads, "modes": [m.name for m in modes],
        "read_only_hints": hint_settings, "backend": args.backend,
        "sample_rate": args.sample_rate, "seed": args.seed, "snapshot": args.snapshot,
        "latency_scale": args.latency_scale, "cost": asdict(cost),
    }, indent=1) + "\n")

    summaries: list[dict[str, Any]] = []
    with open(out / "calls.jsonl", "w") as calls, open(out / "mismatches.jsonl", "w") as bad:
        for workload in workloads:
            for hints in hint_settings:
                for mode in modes:
                    config = RunConfig(
                        backend=args.backend, host=args.host, port=args.port,
                        sample_rate=args.sample_rate, seed=args.seed,
                        snapshot=args.snapshot, cost=cost,
                        latency_scale=args.latency_scale, read_only_hints=hints,
                        keep_values=args.keep_values,
                    )
                    try:
                        result = await run_mode(workload, mode, config)
                    except RuntimeError as error:  # e.g. real canonicalizer missing
                        print(f"skip {workload.name} {mode.name}: {error}", file=sys.stderr)
                        continue
                    summary = summarize(result, workload, config)
                    summaries.append(summary)
                    for row in record_rows(result, workload):
                        row["read_only_hints"] = hints
                        line = json.dumps(row) + "\n"
                        calls.write(line)
                        if row["matched"] is False:
                            bad.write(line)
                    print(
                        f"{workload.name:>10} hints={'on ' if hints else 'off'} "
                        f"{mode.name:<24} hit={summary['hit_rate']:.3f} "
                        f"(exact {summary['exact_hits']}, fuzzy {summary['fuzzy_hits']}) "
                        f"false={summary['false_fuzzy_hits']}/{summary['validated_fuzzy_hits']} "
                        f"execs={summary['physical_executions']} "
                        f"model={summary['modeled_tool_seconds']:.1f}s"
                    )
    (out / "summary.json").write_text(json.dumps(summaries, indent=1) + "\n")
    with open(out / "summary.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summaries)
    print(f"results in {out}")
    return out


def _plot(args: argparse.Namespace) -> None:
    from fuzzytv.eval.plots import plot_run

    for path in plot_run(Path(args.run_dir)):
        print(f"wrote {path}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m fuzzytv.eval")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="write a reproducible workload trace")
    gen.add_argument("--kind", choices=sorted(GENERATORS), required=True)
    gen.add_argument("-o", "--output", required=True)
    gen.add_argument("--name")
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--tasks", type=int, default=6)
    gen.add_argument("--rollouts", type=int, default=8)
    defaults = Perturbation()
    for field_name in ("p_variant", "p_noise", "p_drop", "p_stop"):
        gen.add_argument(
            f"--{field_name.replace('_', '-')}", dest=field_name, type=float,
            default=getattr(defaults, field_name),
        )

    run = sub.add_parser("run", help="run workloads under cache modes")
    run.add_argument("workloads", nargs="+")
    run.add_argument(
        "--modes", nargs="+",
        help=f"kind[:canonicalizer]; kinds none|exact|canonical|relevant_state, "
        f"canonicalizers oracle|loose|real|real-loose. Default: {' '.join(DEFAULT_MODES)}",
    )
    run.add_argument("--read-only-hints", choices=["off", "on", "both"], default="both")
    run.add_argument("--backend", choices=["memory", "server"], default="memory")
    run.add_argument("--host", default=os.environ.get("TVCACHE_HOST", "127.0.0.1"))
    run.add_argument("--port", type=int, default=int(os.environ.get("TVCACHE_PORT", "7878")))
    run.add_argument("--sample-rate", type=float, default=1.0,
                     help="fraction of rollouts validated against a reference sandbox")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--snapshot", choices=["adaptive", "always", "never"], default="adaptive")
    run.add_argument("--latency-scale", type=float, default=0.01)
    run.add_argument("--cost", help='JSON overrides, e.g. \'{"execute": 0.2, "start": 5}\'')
    run.add_argument("--keep-values", action="store_true",
                     help="store every returned/reference value in calls.jsonl")
    run.add_argument("--out", help="output root (default: $FUZZYTV_OUT)")
    run.add_argument("--name", help="run directory name (default: timestamp)")
    run.add_argument("--plot", action="store_true", help="also write plots")

    plot = sub.add_parser("plot", help="plot a finished run directory")
    plot.add_argument("run_dir")

    args = parser.parse_args(argv)
    if args.command == "generate":
        _generate(args)
    elif args.command == "run":
        out = asyncio.run(_run(args))
        if args.plot:
            _plot(argparse.Namespace(run_dir=out))
    else:
        _plot(args)
