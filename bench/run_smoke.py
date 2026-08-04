"""Smoke runner: run a small benchmark matrix, score it, and write a report.

Usage:
    python3 -m bench.run_smoke --n 5 --systems structured,flat,longctx
    python3 -m bench.run_smoke --n 1 --systems flat --skip-mab --judge-model gpt-4o-mini
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bench.config import AGENT_MODEL, JUDGE_MODEL, RESULTS_DIR, DatasetCacheConfig, ensure_llm_credentials
from bench.data import longmemeval, memoryagentbench
from bench.harness import report, runner, scoring
from bench.ir.types import BenchmarkInstance
from bench.llm.client import LLMClient


# Rough per-instance cost estimates used only for the pre-flight cap check.
# LongMemEval instances are 1 question each; MAB rows are ~100 questions each.
_LONGMEMEVAL_COST_PER_INSTANCE = 0.10  # USD
_MAB_ROW_COST = 0.60  # USD


def _select_longmemeval_smoke_indices(n: int, min_knowledge_update: int = 2) -> list[int]:
    """Deterministically select n LongMemEval instances with at least k knowledge-update ones.

    Selection rules:
    1. Take the first min(min_knowledge_update, n) knowledge-update instances in the file order.
    2. Fill the remaining slots with the earliest non-knowledge-update instances in file order.
    3. Return the selected indices sorted by their original position.
    """
    path = DatasetCacheConfig().longmemeval_dir / "longmemeval_s_cleaned.json"
    rows = longmemeval.load_rows(path)
    ku_indices = [i for i, row in enumerate(rows) if row.get("question_type") == "knowledge-update"]
    selected = set(ku_indices[: min(min_knowledge_update, n)])
    remaining = n - len(selected)
    for i, row in enumerate(rows):
        if remaining <= 0:
            break
        if i in selected:
            continue
        if row.get("question_type") == "knowledge-update":
            continue
        selected.add(i)
        remaining -= 1
    return sorted(selected)


def _projected_cost(n: int, systems: list[str], skip_mab: bool) -> float:
    """Return a conservative cost estimate for the smoke matrix."""
    longmemeval_cost = n * len(systems) * _LONGMEMEVAL_COST_PER_INSTANCE
    if skip_mab:
        return longmemeval_cost
    mab_rows = 0
    # Conflict_Resolution: structured + flat only, but only if those systems are requested.
    for name in ("structured", "flat"):
        if name in systems:
            mab_rows += 1
    # Accurate_Retrieval: all requested systems.
    mab_rows += len(systems)
    return longmemeval_cost + mab_rows * _MAB_ROW_COST


def _run_smoke(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a small benchmark smoke matrix")
    parser.add_argument("--n", type=int, default=5, help="Number of LongMemEval instances")
    parser.add_argument(
        "--systems",
        default="structured,flat,longctx",
        help="Comma-separated systems to evaluate",
    )
    parser.add_argument("--skip-mab", action="store_true", help="Skip MemoryAgentBench")
    parser.add_argument("--max-cost", type=float, default=5.0, help="Hard cost cap in USD")
    parser.add_argument("--out", default=str(RESULTS_DIR / "smoke"), help="Output directory")
    parser.add_argument(
        "--judge-model",
        default=JUDGE_MODEL,
        help="Judge model for LongMemEval scoring",
    )
    args = parser.parse_args(argv)

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    if not systems:
        print("No systems specified.", file=sys.stderr)
        return 1

    projected = _projected_cost(args.n, systems, args.skip_mab)
    if projected > args.max_cost:
        print(
            f"Projected cost ${projected:.2f} exceeds the hard cap of ${args.max_cost:.2f}. "
            f"Reduce --n, --systems, or raise --max-cost.",
            file=sys.stderr,
        )
        return 1

    ensure_llm_credentials()
    agent_client = LLMClient(model=AGENT_MODEL, temperature=0)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # LongMemEval
    # -----------------------------------------------------------------------
    selected_indices = _select_longmemeval_smoke_indices(args.n)
    all_instances = list(longmemeval.iter_instances())
    selected_instances = [all_instances[i] for i in selected_indices]
    print(f"LongMemEval: selected {len(selected_instances)} instances at indices {selected_indices}")

    for system_name in systems:
        print(f"Running LongMemEval / {system_name} ...", file=sys.stderr)
        config: dict[str, Any] = {
            "dataset": "longmemeval",
            "system": system_name,
            "instances": selected_instances,
            "out": str(out_dir),
            "temperature": 0.0,
            "model": AGENT_MODEL,
            "client": agent_client,
        }
        runner.run(config)
        if agent_client.ledger.total_cost_usd > args.max_cost:
            print(
                f"Cost ${agent_client.ledger.total_cost_usd:.2f} exceeded the cap of ${args.max_cost:.2f}. "
                "Aborting smoke run.",
                file=sys.stderr,
            )
            return 1

    # -----------------------------------------------------------------------
    # MemoryAgentBench
    # -----------------------------------------------------------------------
    if not args.skip_mab:
        # Conflict_Resolution: 1 row, structured + flat only.
        cr_instances = list(memoryagentbench.iter_instances(split="Conflict_Resolution", max_instances=1))
        for system_name in ("structured", "flat"):
            if system_name not in systems:
                continue
            print(f"Running MAB Conflict_Resolution / {system_name} ...", file=sys.stderr)
            config = {
                "dataset": "mab",
                "split": "Conflict_Resolution",
                "system": system_name,
                "instances": cr_instances,
                "out": str(out_dir),
                "client": agent_client,
            }
            runner.run(config)
            if agent_client.ledger.total_cost_usd > args.max_cost:
                print(
                    f"Cost ${agent_client.ledger.total_cost_usd:.2f} exceeded the cap of ${args.max_cost:.2f}. "
                    "Aborting smoke run.",
                    file=sys.stderr,
                )
                return 1

        # Accurate_Retrieval: 1 row, all requested systems.
        ar_instances = list(memoryagentbench.iter_instances(split="Accurate_Retrieval", max_instances=1))
        for system_name in systems:
            print(f"Running MAB Accurate_Retrieval / {system_name} ...", file=sys.stderr)
            config = {
                "dataset": "mab",
                "split": "Accurate_Retrieval",
                "system": system_name,
                "instances": ar_instances,
                "out": str(out_dir),
                "client": agent_client,
            }
            runner.run(config)
            if agent_client.ledger.total_cost_usd > args.max_cost:
                print(
                    f"Cost ${agent_client.ledger.total_cost_usd:.2f} exceeded the cap of ${args.max_cost:.2f}. "
                    "Aborting smoke run.",
                    file=sys.stderr,
                )
                return 1

    # -----------------------------------------------------------------------
    # Score each run
    # -----------------------------------------------------------------------
    judge_client = LLMClient(model=args.judge_model, temperature=0)
    run_dirs: list[Path] = []
    for dataset_dir in sorted(out_dir.iterdir()):
        if not dataset_dir.is_dir():
            continue
        for system_dir in sorted(dataset_dir.iterdir()):
            if not system_dir.is_dir():
                continue
            if not (system_dir / "metrics.json").exists():
                run_dirs.append(system_dir)
                scoring.score_run(system_dir, judge_client=judge_client)

    # -----------------------------------------------------------------------
    # Combined report
    # -----------------------------------------------------------------------
    report.write_report(run_dirs, out_dir / "report.md")

    total_cost = agent_client.ledger.total_cost_usd + judge_client.ledger.total_cost_usd
    print(f"Total cost: ${total_cost:.4f}")
    print(f"Report written to {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(_run_smoke())
