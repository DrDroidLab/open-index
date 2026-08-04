"""Sharded full-run wrapper for the benchmark harness.

Spawns worker processes that each run a deterministic shard of the evaluation
instances via `bench.harness.runner.run`, then merges the shard outputs into a
single run directory and scores it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from bench.config import (
    AGENT_MODEL,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    JUDGE_MODEL,
    RESULTS_DIR,
    TEMPERATURE,
    ensure_llm_credentials,
)
from bench.data import longmemeval, memoryagentbench
from bench.harness import report, runner, scoring
from bench.ir.types import BenchmarkInstance, read_instances, write_instances
from bench.llm.client import LLMClient


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a sharded full benchmark evaluation"
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--config-path",
        type=str,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dataset",
        choices=["longmemeval", "mab"],
        help="Dataset to evaluate",
    )
    parser.add_argument(
        "--split",
        default=None,
        help="MAB split (Accurate_Retrieval or Conflict_Resolution)",
    )
    parser.add_argument(
        "--source-filter",
        default=None,
        help="Filter rows by source or question_type substring",
    )
    parser.add_argument(
        "--system",
        choices=["structured", "flat", "longctx"],
        help="System arm to evaluate",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of worker processes (default 8)",
    )
    parser.add_argument(
        "--max-instances",
        type=int,
        default=None,
        help="Limit total instances across all shards",
    )
    parser.add_argument(
        "--out",
        default=str(RESULTS_DIR / "full"),
        help="Output directory for shards and merged results",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Retrieval budget (default 5)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="LLM seed for deterministic tool loops",
    )
    parser.add_argument(
        "--judge-model",
        default=JUDGE_MODEL,
        help="Judge model for LongMemEval scoring",
    )
    parser.add_argument(
        "--no-score",
        action="store_true",
        help="Skip scoring and report generation after merging",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=0.0,
        help="Approximate per-worker cost cap in USD (0 = unlimited)",
    )
    parser.add_argument(
        "--model",
        default=AGENT_MODEL,
        help="Agent model for the memory systems",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=TEMPERATURE,
        help="Sampling temperature for the agent",
    )
    args = parser.parse_args(argv)
    if not args.worker:
        if not args.dataset or not args.system:
            parser.error("--dataset and --system are required")
        if args.dataset == "mab" and not args.split:
            parser.error("--split is required for --dataset mab")
    return args


def load_instances(args: argparse.Namespace) -> list[BenchmarkInstance]:
    """Load instances from the existing adapters with the given filters."""
    if args.dataset == "longmemeval":
        return list(
            longmemeval.iter_instances(
                max_instances=args.max_instances,
                source_filter=args.source_filter,
            )
        )
    if args.dataset == "mab":
        return list(
            memoryagentbench.iter_instances(
                split=args.split,
                max_instances=args.max_instances,
                source_filter=args.source_filter,
            )
        )
    raise ValueError(f"Unknown dataset: {args.dataset}")


def shard_instances(
    instances: list[BenchmarkInstance], workers: int
) -> list[list[BenchmarkInstance]]:
    """Deterministically shard instances by index: shard i gets instances[i::P]."""
    workers = max(1, workers)
    return [instances[i::workers] for i in range(workers)]


def _worker_entry(config: dict[str, Any]) -> int:
    """Internal worker entry: run one shard and write its outputs."""
    out_dir = Path(config["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    instances_path = Path(config["instances_path"])
    instances = list(read_instances(instances_path))

    # Resume: skip question_ids already present in the shard's predictions.
    done_qids = runner._existing_qids_from_predictions(out_dir / "predictions.jsonl")
    todo: list[BenchmarkInstance] = []
    skipped = 0
    for instance in instances:
        if any(q.question_id in done_qids for q in instance.questions):
            skipped += 1
        else:
            todo.append(instance)

    if skipped:
        print(f"Worker resuming: skipping {skipped} already-completed instances")
    if not todo:
        print("Worker: all instances already completed")

    worker_config = {
        "dataset": config["dataset"],
        "system": config["system"],
        "split": config.get("split", ""),
        "source_filter": config.get("source_filter"),
        "out_dir": str(out_dir),
        "instances": todo,
        "resume": True,
        "cost_cap": config.get("cost_cap", 0.0),
        "k": config["k"],
        "seed": config["seed"],
        "model": config.get("model", AGENT_MODEL),
        "temperature": config.get("temperature", TEMPERATURE),
    }
    summary = runner.run(worker_config)
    print(json.dumps(summary, indent=2))
    return 0


def _merge_shard_outputs(merged_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Concatenate shard predictions and metadata into merged_dir."""
    merged_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = merged_dir / "predictions.jsonl"
    metadata_path = merged_dir / "metadata.jsonl"

    with predictions_path.open("w", encoding="utf-8") as preds_out, metadata_path.open(
        "w", encoding="utf-8"
    ) as meta_out:
        for shard_dir in sorted(out_dir.glob("shard_*")):
            shard_preds = shard_dir / "predictions.jsonl"
            shard_meta = shard_dir / "metadata.jsonl"
            if shard_preds.exists():
                with shard_preds.open("r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            preds_out.write(line)
            if shard_meta.exists():
                with shard_meta.open("r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            meta_out.write(line)

    # Merge per-shard update/contradiction probe results (disjoint by instance,
    # so plain concatenation is correct). Scoring reads merged/probe_results.json.
    merged_probes: list[dict[str, Any]] = []
    for shard_dir in sorted(out_dir.glob("shard_*")):
        shard_probes = shard_dir / "probe_results.json"
        if shard_probes.exists():
            try:
                data = json.loads(shard_probes.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                merged_probes.extend(data)
    probe_results_path = merged_dir / "probe_results.json"
    if merged_probes:
        probe_results_path.write_text(
            json.dumps(merged_probes, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    total_instances = 0
    total_questions = 0
    total_failures = 0
    total_cost = 0.0
    for shard_dir in sorted(out_dir.glob("shard_*")):
        summary_path = shard_dir / "summary.json"
        if not summary_path.exists():
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        total_instances += int(summary.get("instances", 0))
        total_questions += int(summary.get("questions", 0))
        total_failures += int(summary.get("failures", 0))
        total_cost += float(summary.get("total_cost_usd", 0.0))

    merged_summary = {
        "dataset": "",
        "system": "",
        "split": "",
        "instances": total_instances,
        "questions": total_questions,
        "failures": total_failures,
        "k": 5,
        "seed": 42,
        "total_cost_usd": round(total_cost, 6),
        "predictions_path": str(predictions_path),
        "metadata_path": str(metadata_path),
        "probe_results_path": str(probe_results_path) if merged_probes else None,
    }
    merged_summary_path = merged_dir / "summary.json"
    merged_summary_path.write_text(
        json.dumps(merged_summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return merged_summary


def merge_shards(
    out_dir: Path,
    dataset: str,
    system: str,
    split: str,
    k: int,
    seed: int,
) -> dict[str, Any]:
    """Merge shard outputs and write a merged summary.json."""
    merged_dir = out_dir / "merged"
    merged_summary = _merge_shard_outputs(merged_dir, out_dir)
    merged_summary["dataset"] = dataset
    merged_summary["system"] = system
    merged_summary["split"] = split
    merged_summary["k"] = k
    merged_summary["seed"] = seed
    (merged_dir / "summary.json").write_text(
        json.dumps(merged_summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return merged_summary


def _score_merged(merged_dir: Path, judge_model: str) -> dict[str, Any]:
    """Score the merged run directory and write a combined report."""
    judge_client = LLMClient(
        model=judge_model,
        temperature=0,
        endpoint=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_API_KEY,
    )
    metrics = scoring.score_run(merged_dir, judge_client=judge_client)
    return metrics


def _build_worker_configs(
    args: argparse.Namespace,
    shards: list[list[BenchmarkInstance]],
    out_dir: Path,
) -> list[dict[str, Any]]:
    """Build a config dict for each worker shard."""
    cost_cap = 0.0
    if args.max_cost > 0:
        cost_cap = args.max_cost / max(1, args.workers)

    configs: list[dict[str, Any]] = []
    for i, shard in enumerate(shards):
        shard_dir = out_dir / f"shard_{i}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        instances_path = shard_dir / "instances.jsonl"
        write_instances(instances_path, shard)
        config: dict[str, Any] = {
            "dataset": args.dataset,
            "system": args.system,
            "split": args.split or "",
            "source_filter": args.source_filter,
            "out_dir": str(shard_dir),
            "instances_path": str(instances_path),
            "k": args.k,
            "seed": args.seed,
            "model": args.model,
            "temperature": args.temperature,
            "cost_cap": cost_cap,
        }
        configs.append(config)
    return configs


def _spawn_worker(config: dict[str, Any], log_file: Path) -> subprocess.Popen:
    """Spawn a worker subprocess and stream its output to a log file."""
    shard_dir = Path(config["out_dir"])
    config_path = shard_dir / "worker_config.json"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bench.run_full",
            "--worker",
            "--config-path",
            str(config_path),
        ],
        stdout=log_file.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        env={**dict(os.environ)},
    )
    return proc


def run_full(argv: list[str] | None = None) -> int:
    """Orchestrate a sharded full evaluation."""
    args = parse_args(argv)
    ensure_llm_credentials()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    instances = load_instances(args)
    if not instances:
        print("No instances loaded.", file=sys.stderr)
        return 1

    shards = shard_instances(instances, args.workers)
    configs = _build_worker_configs(args, shards, out_dir)

    procs: list[tuple[int, subprocess.Popen]] = []
    for i, config in enumerate(configs):
        log_file = logs_dir / f"shard_{i}.log"
        print(f"Starting worker {i} for {len(shards[i])} instances -> {config['out_dir']}")
        proc = _spawn_worker(config, log_file)
        procs.append((i, proc))

    failures: list[int] = []
    for i, proc in procs:
        exit_code = proc.wait()
        if exit_code != 0:
            failures.append(i)
            print(
                f"Worker {i} failed with exit code {exit_code} (log: {logs_dir / f'shard_{i}.log'})",
                file=sys.stderr,
            )
        else:
            print(f"Worker {i} finished successfully")

    merged_summary = merge_shards(
        out_dir,
        dataset=args.dataset,
        system=args.system,
        split=args.split or "",
        k=args.k,
        seed=args.seed,
    )

    if not args.no_score:
        merged_dir = out_dir / "merged"
        metrics = _score_merged(merged_dir, args.judge_model)
        report.write_report([merged_dir], out_dir / "report.md")
        print(f"Merged questions: {merged_summary['questions']}")
        print(f"Total cost: ${merged_summary['total_cost_usd']:.4f}")
        print(f"Judge cost: ${metrics.get('judge_cost_usd', 0.0):.4f}")
        if "judge" in metrics:
            print(f"LongMemEval judge accuracy: {metrics['judge']['overall']:.2%}")
        if "subem" in metrics:
            print(f"MAB SubEM: {metrics['subem']['overall']:.2%}")
    else:
        print(f"Merged questions: {merged_summary['questions']}")
        print(f"Total cost: ${merged_summary['total_cost_usd']:.4f}")

    if failures:
        print(
            f"WARNING: {len(failures)} worker(s) failed: {failures}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.worker:
        if not args.config_path:
            raise ValueError("--config-path is required for --worker")
        config = json.loads(Path(args.config_path).read_text(encoding="utf-8"))
        return _worker_entry(config)
    return run_full(argv)


if __name__ == "__main__":
    sys.exit(main())
