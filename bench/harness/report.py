"""Markdown report generator for scored benchmark runs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench.config import RESULTS_DIR


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2%}"


def _fmt_usd(value: float) -> str:
    return f"${value:.4f}"


def _fmt_num(value: float) -> str:
    return f"{value:.2f}"


def _load_metrics(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "metrics.json"
    if not path.exists():
        raise FileNotFoundError(f"No metrics.json in {run_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def _build_pivot_table(
    metrics_list: list[dict[str, Any]],
    *,
    dataset: str,
    metric_key: str,
    overall_key: str,
    per_key: str,
    row_header: str,
) -> str:
    """Build a system x metric-key markdown table from a list of metrics dicts."""
    filtered = [m for m in metrics_list if m["dataset"] == dataset]
    if not filtered:
        return ""

    systems = sorted({m["system"] for m in filtered})
    all_keys: set[str] = set()
    per_system: dict[str, dict[str, float]] = defaultdict(dict)
    overall: dict[str, float] = {}
    for m in filtered:
        system = m["system"]
        metric = m.get(metric_key, {})
        overall[system] = metric.get(overall_key, 0.0)
        for key, acc in metric.get(per_key, {}).items():
            all_keys.add(key)
            per_system[system][key] = acc

    rows: list[str] = []
    header = [row_header] + systems
    rows.append("| " + " | ".join(header) + " |")
    rows.append("| " + " | ".join(["---"] * len(header)) + " |")

    for key in sorted(all_keys):
        row = [key]
        for system in systems:
            row.append(_fmt_pct(per_system[system].get(key)))
        rows.append("| " + " | ".join(row) + " |")

    overall_row = ["**Overall**"]
    for system in systems:
        overall_row.append(_fmt_pct(overall.get(system)))
    rows.append("| " + " | ".join(overall_row) + " |")

    return "\n".join(rows)


def _build_accuracy_table(metrics_list: list[dict[str, Any]]) -> str:
    """Build a per-ability × system accuracy table from LongMemEval judge metrics."""
    return _build_pivot_table(
        metrics_list,
        dataset="longmemeval",
        metric_key="judge",
        overall_key="overall",
        per_key="per_question_type",
        row_header="Ability / System",
    )


def _build_subem_table(metrics_list: list[dict[str, Any]]) -> str:
    """Build a SubEM table for MemoryAgentBench runs."""
    return _build_pivot_table(
        metrics_list,
        dataset="mab",
        metric_key="subem",
        overall_key="overall",
        per_key="per_group",
        row_header="Source group / System",
    )


def _build_probe_table(metrics_list: list[dict[str, Any]]) -> str:
    """Build an update-correctness probe table."""
    rows = []
    header = ["Dataset / System", "Probe count", "Update-correctness"]
    rows.append("| " + " | ".join(header) + " |")
    rows.append("| --- | --- | --- |")

    for m in sorted(metrics_list, key=lambda x: (x["dataset"], x["system"])):
        probes = m.get("probes", {})
        count = probes.get("count", 0)
        acc = probes.get("accuracy")
        rows.append(
            f"| {m['dataset']} / {m['system']} | {count} | {_fmt_pct(acc)} |"
        )
    return "\n".join(rows)


def _build_recall_table(metrics_list: list[dict[str, Any]]) -> str:
    """Build a Recall@k table for LongMemEval."""
    longmemeval = [m for m in metrics_list if m["dataset"] == "longmemeval"]
    if not longmemeval:
        return ""

    rows = []
    header = ["System", "Mean Recall@k", "Evaluated questions"]
    rows.append("| " + " | ".join(header) + " |")
    rows.append("| --- | --- | --- |")

    for m in sorted(longmemeval, key=lambda x: x["system"]):
        recall = m.get("recall_at_k", {})
        rows.append(
            f"| {m['system']} | {_fmt_pct(recall.get('mean'))} | {recall.get('count', 0)} |"
        )
    return "\n".join(rows)


def _build_cost_table(metrics_list: list[dict[str, Any]]) -> str:
    """Build a cost/latency/operations table."""
    rows = []
    header = [
        "Dataset / System",
        "Questions",
        "Total tokens",
        "Total cost",
        "Mean latency (ms)",
        "Tool calls",
        "Truncation rate",
    ]
    rows.append("| " + " | ".join(header) + " |")
    rows.append("| " + " | ".join(["---"] * len(header)) + " |")

    for m in sorted(metrics_list, key=lambda x: (x["dataset"], x["system"])):
        ops = m.get("ops", {})
        rows.append(
            "| "
            + " | ".join(
                [
                    f"{m['dataset']} / {m['system']}",
                    str(m.get("questions", 0)),
                    f"{ops.get('total_tokens', 0):,}",
                    _fmt_usd(ops.get("total_cost_usd", 0.0)),
                    _fmt_num(ops.get("mean_latency_ms", 0.0)),
                    str(ops.get("total_tool_calls", 0)),
                    _fmt_pct(ops.get("truncation_rate", 0.0)),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _build_findings_skeleton() -> str:
    """Return a findings skeleton with the three capability questions."""
    return """## Findings

Fill in the per-capability verdicts after the real benchmark runs. Replace the
placeholders with the observed numbers and cite the table sections above.

### 1. Does structure help retrieval?

- **Verdict:** better / worse / inconclusive (placeholder)
- **Evidence:**
  - LongMemEval overall accuracy (structured vs flat vs long-context): [fill]
  - LongMemEval per-ability accuracy (single-session, multi-session, temporal): [fill]
  - MemoryAgentBench Accurate_Retrieval SubEM (structured vs flat vs long-context): [fill]
  - Recall@k (structured vs flat): [fill]
- **Caveats:** [e.g., LongMemEval is persona-flavored, not org knowledge; overlap with MAB Accurate_Retrieval]

### 2. Does structure help updating?

- **Verdict:** better / worse / inconclusive (placeholder)
- **Evidence:**
  - LongMemEval knowledge-update accuracy (structured vs flat vs long-context): [fill]
  - Update-probe correctness (structured vs flat): [fill]
- **Caveats:** [e.g., agent model is gpt-4o-mini; schema-write failures would understate the structured arm]

### 3. Does structure help contradiction resolution?

- **Verdict:** better / worse / inconclusive (placeholder)
- **Evidence:**
  - MemoryAgentBench Conflict_Resolution SubEM, overall and SH/MH splits (structured vs flat): [fill]
  - Update-probe correctness on conflict-heavy questions (structured vs flat): [fill]
- **Caveats:** [e.g., Conflict_Resolution has SH and MH sub-groups; long-context is skipped for probes]
"""


def build_report(metrics_list: list[dict[str, Any]]) -> str:
    """Build a combined markdown report from a list of metrics dicts."""
    lines: list[str] = []
    lines.append("# droid-brain benchmark report")
    lines.append("")
    lines.append(f"**Systems evaluated:** {', '.join(sorted({m['system'] for m in metrics_list}))}")
    lines.append(f"**Datasets evaluated:** {', '.join(sorted({m['dataset'] for m in metrics_list}))}")
    lines.append("")

    accuracy_table = _build_accuracy_table(metrics_list)
    if accuracy_table:
        lines.append("## Per-ability accuracy (LongMemEval official judge)")
        lines.append("")
        lines.append(accuracy_table)
        lines.append("")

    subem_table = _build_subem_table(metrics_list)
    if subem_table:
        lines.append("## MemoryAgentBench SubEM")
        lines.append("")
        lines.append(subem_table)
        lines.append("")

    lines.append("## Update / contradiction probe correctness")
    lines.append("")
    lines.append(_build_probe_table(metrics_list))
    lines.append("")

    recall_table = _build_recall_table(metrics_list)
    if recall_table:
        lines.append("## Recall@k (LongMemEval)")
        lines.append("")
        lines.append(recall_table)
        lines.append("")

    lines.append("## Cost, latency, and operations")
    lines.append("")
    lines.append(_build_cost_table(metrics_list))
    lines.append("")

    # Truncation note
    longctx = [m for m in metrics_list if m["system"] == "longctx"]
    if longctx:
        lines.append("## Truncation note")
        lines.append("")
        for m in longctx:
            rate = m.get("ops", {}).get("truncation_rate", 0.0)
            lines.append(
                f"- **{m['dataset']} / long-context:** truncation rate = {_fmt_pct(rate)} "
                f"({m.get('questions', 0)} questions)."
            )
        lines.append("")

    lines.append(_build_findings_skeleton())
    return "\n".join(lines)


def write_report(
    run_dirs: list[Path | str],
    out_path: Path | str,
) -> str:
    """Load metrics from run_dirs and write a combined markdown report."""
    metrics_list = [_load_metrics(Path(d)) for d in run_dirs]
    report = build_report(metrics_list)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a combined markdown report from scored runs")
    parser.add_argument(
        "--runs",
        required=True,
        nargs="+",
        help="One or more scored run directories containing metrics.json",
    )
    parser.add_argument(
        "--out",
        default=str(RESULTS_DIR / "report.md"),
        help="Output markdown path (default: bench/results/report.md)",
    )
    args = parser.parse_args(argv)

    write_report(args.runs, args.out)
    print(f"Report written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
