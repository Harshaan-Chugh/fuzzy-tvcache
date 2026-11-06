"""Static report figures from a run directory's summary.json (needs matplotlib).

Install with `pip install -e '.[eval]'`. Three figures, faceted by workload:

1. hit_rates.png: hit rate per mode, split into exact and fuzzy hits.
2. false_hits.png: measured false-hit rate with 95% Wilson CI, per mode.
3. executions.png: physical tool executions relative to `none`.

Hints off and on are separate rows when both were run.
"""

import json
from pathlib import Path
from typing import Any

# Reference palette (dataviz skill, light mode), categorical slots in fixed order.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SLOT = ["#2a78d6", "#eb6834", "#1baf7a"]


def _style(plt: Any) -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE, "axes.edgecolor": AXIS,
        "axes.labelcolor": INK_2, "xtick.color": MUTED, "ytick.color": INK_2,
        "text.color": INK, "axes.grid": True, "grid.color": GRID,
        "grid.linewidth": 0.6, "axes.spines.top": False, "axes.spines.right": False,
        "font.size": 9, "axes.titlesize": 10, "axes.titleweight": "bold",
        "legend.frameon": False,
    })


def _facets(summaries: list[dict[str, Any]]) -> tuple[list[str], list[bool]]:
    workloads = list(dict.fromkeys(s["workload"] for s in summaries))
    hints = sorted({bool(s.get("read_only_hints")) for s in summaries})
    return workloads, hints


def _select(summaries: list[dict[str, Any]], workload: str, hints: bool) -> list[dict[str, Any]]:
    return [
        s for s in summaries
        if s["workload"] == workload and bool(s.get("read_only_hints")) == hints
    ]


def _title(workload: str, hints: bool, both: bool) -> str:
    return f"{workload}" + (f" · read-only hints {'on' if hints else 'off'}" if both else "")


def _top_legend(fig: Any, plt: Any, hint_rows: list[bool], both: bool) -> None:
    if not both:
        return
    from matplotlib.patches import Patch

    fig.legend(
        handles=[Patch(color=SLOT[i], label=f"read-only hints {'on' if h else 'off'}")
                 for i, h in enumerate(hint_rows)],
        loc="upper right", ncol=len(hint_rows), fontsize=8,
    )


def plot_run(run_dir: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.patches
    import matplotlib.pyplot as plt
    import matplotlib.ticker

    _style(plt)
    summaries = json.loads((run_dir / "summary.json").read_text())
    workloads, hint_rows = _facets(summaries)
    both = len(hint_rows) > 1
    rows, cols = len(hint_rows), len(workloads)
    written = []

    # 1. Hit rate, stacked exact + fuzzy (2px surface gap between segments).
    fig, axes = plt.subplots(rows, cols, figsize=(4.6 * cols, 2.6 * rows),
                             squeeze=False, sharex=True)
    for r, hints in enumerate(hint_rows):
        for c, workload in enumerate(workloads):
            ax = axes[r][c]
            data = _select(summaries, workload, hints)
            labels = [s["mode"] for s in data][::-1]
            exact = [s["exact_hits"] / s["calls"] for s in data][::-1]
            fuzzy = [s["fuzzy_hits"] / s["calls"] for s in data][::-1]
            y = range(len(labels))
            ax.barh(y, exact, height=0.55, color=SLOT[0], edgecolor=SURFACE,
                    linewidth=2, label="exact hit")
            ax.barh(y, fuzzy, left=exact, height=0.55, color=SLOT[1],
                    edgecolor=SURFACE, linewidth=2, label="fuzzy hit")
            for yi, (e, f) in enumerate(zip(exact, fuzzy, strict=True)):
                ax.text(e + f + 0.01, yi, f"{e + f:.0%}", va="center", color=INK_2,
                        fontsize=8)
            ax.set_yticks(list(y), labels)
            ax.set_xlim(0, 1)
            ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
            ax.grid(axis="y", visible=False)
            ax.set_title(_title(workload, hints, both), loc="left")
    fig.legend(
        handles=[matplotlib.patches.Patch(color=SLOT[0], label="exact hit"),
                 matplotlib.patches.Patch(color=SLOT[1], label="fuzzy hit")],
        loc="upper right", ncol=2, fontsize=8,
    )
    fig.supxlabel("share of tool calls served from cache", color=INK_2, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    written.append(run_dir / "hit_rates.png")
    fig.savefig(written[-1], dpi=200)
    plt.close(fig)

    # 2. False-hit rate with 95% Wilson CI, for modes that served fuzzy hits.
    fig, axes = plt.subplots(1, cols, figsize=(4.6 * cols, 2.8), squeeze=False)
    for c, workload in enumerate(workloads):
        ax = axes[0][c]
        modes = list(dict.fromkeys(
            s["mode"] for s in summaries
            if s["workload"] == workload and s["validated_fuzzy_hits"]
        ))[::-1]
        offset = 0.18 if both else 0.0
        for i, hints in enumerate(hint_rows):
            data = {s["mode"]: s for s in _select(summaries, workload, hints)}
            for m, mode in enumerate(modes):
                s = data.get(mode)
                if not s or not s["validated_fuzzy_hits"]:
                    continue
                y = m + (offset if i == 0 else -offset)
                low, high = s["false_hit_rate_ci95"]
                ax.plot([low, high], [y, y], color=SLOT[i], lw=2, solid_capstyle="round")
                ax.plot([s["false_hit_rate"]], [y], "o", ms=7, color=SLOT[i],
                        mec=SURFACE, mew=2)
                ax.text(high + 0.008, y,
                        f"{s['false_fuzzy_hits']}/{s['validated_fuzzy_hits']} false"
                        f" · hit rate {s['hit_rate']:.0%}",
                        va="center", fontsize=7, color=INK_2)
        ax.set_yticks(range(len(modes)), modes)
        ax.set_ylim(-0.6, len(modes) - 0.4)
        ax.set_xlim(0, 0.5)
        ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("false-hit rate of validated fuzzy hits (95% CI)")
        ax.set_title(workload, loc="left")
    _top_legend(fig, plt, hint_rows, both)
    fig.tight_layout(rect=(0, 0, 1, 0.9 if both else 1))
    written.append(run_dir / "false_hits.png")
    fig.savefig(written[-1], dpi=200)
    plt.close(fig)

    # 3. Physical executions relative to no caching.
    fig, axes = plt.subplots(1, cols, figsize=(4.6 * cols, 2.8), squeeze=False)
    for c, workload in enumerate(workloads):
        ax = axes[0][c]
        modes = [s["mode"] for s in _select(summaries, workload, hint_rows[0])]
        width = 0.8 / len(hint_rows)
        for i, hints in enumerate(hint_rows):
            data = {s["mode"]: s for s in _select(summaries, workload, hints)}
            base = data.get("none", {}).get("physical_executions") or 1
            values = [data[m]["physical_executions"] / base if m in data else 0 for m in modes]
            xs = [m + (i - (len(hint_rows) - 1) / 2) * width for m in range(len(modes))]
            ax.bar(xs, values, width=width, color=SLOT[i], edgecolor=SURFACE, linewidth=2)
            for x, v in zip(xs, values, strict=True):
                ax.text(x, v + 0.02, f"{v:.2f}", ha="center", fontsize=7, color=INK_2)
        ax.set_xticks(range(len(modes)),
                      [m.replace("relevant_state", "rs") for m in modes],
                      rotation=20, ha="right")
        ax.set_ylim(0, 1.15)
        ax.set_ylabel("executions ÷ none")
        ax.grid(axis="x", visible=False)
        ax.set_title(workload, loc="left")
    _top_legend(fig, plt, hint_rows, both)
    fig.tight_layout(rect=(0, 0, 1, 0.88 if both else 1))
    written.append(run_dir / "executions.png")
    fig.savefig(written[-1], dpi=200)
    plt.close(fig)
    return written
