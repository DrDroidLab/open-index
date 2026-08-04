"""Benchmark runner: dataset x system matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

from bench.config import (
    AGENT_MODEL,
    BENCH_DIR,
    RESULTS_DIR,
    TEMPERATURE,
    ensure_llm_credentials,
)
from bench.data import longmemeval, memoryagentbench
from bench.ir.types import BenchmarkInstance, Question
from bench.llm.client import LLMClient
from bench.systems import FlatMemoryBaseline, LongContextBaseline, StructuredBrainMemory
from bench.systems.base import Answer, MemorySystem


SYSTEMS: dict[str, Callable[[LLMClient], MemorySystem]] = {
    "structured": lambda client: StructuredBrainMemory(client),
    "flat": lambda client: FlatMemoryBaseline(client),
    "longctx": lambda client: LongContextBaseline(client),
}


def _load_instances(config: dict[str, Any]) -> Iterator[BenchmarkInstance]:
    dataset = config["dataset"]
    max_instances = config.get("max_instances")
    source_filter = config.get("source_filter")
    if dataset == "longmemeval":
        yield from longmemeval.iter_instances(
            max_instances=max_instances, source_filter=source_filter
        )
    elif dataset == "mab":
        split = config.get("split")
        if not split:
            raise ValueError("--split is required for MemoryAgentBench")
        yield from memoryagentbench.iter_instances(
            split=split, max_instances=max_instances, source_filter=source_filter
        )
    else:
        raise ValueError(f"Unknown dataset: {dataset}")


def _make_system(config: dict[str, Any]) -> MemorySystem:
    system_name = config["system"]
    factory = SYSTEMS.get(system_name)
    if factory is None:
        raise ValueError(f"Unknown system: {system_name}")
    client = LLMClient(
        model=config.get("model", AGENT_MODEL),
        temperature=config.get("temperature", TEMPERATURE),
    )
    return factory(client)


def _run_instance(system: MemorySystem, instance: BenchmarkInstance) -> list[tuple[Question, Answer]]:
    system.ingest(iter(instance.events))
    results: list[tuple[Question, Answer]] = []
    for question in instance.questions:
        answer = system.answer(question)
        results.append((question, answer))
    return results


def _write_longmemeval_predictions(path: Path, results: list[tuple[Question, Answer]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for question, answer in results:
            line = {
                "question_id": question.question_id,
                "hypothesis": answer.text,
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _write_mab_predictions(path: Path, results: list[tuple[Question, Answer]], split: str, source: str) -> None:
    with path.open("w", encoding="utf-8") as f:
        for question, answer in results:
            line = {
                "qa_pair_id": question.question_id,
                "hypothesis": answer.text,
                "gold_answers": question.gold_answers,
                "source": source,
                "split": split,
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _write_metadata(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run(config: dict[str, Any]) -> dict[str, Any]:
    """Run one dataset x system combination and write result files."""
    dataset = config["dataset"]
    system_name = config["system"]
    out_dir = Path(config.get("out", RESULTS_DIR)) / dataset / system_name
    out_dir.mkdir(parents=True, exist_ok=True)

    predictions_path = out_dir / "predictions.jsonl"
    metadata_path = out_dir / "metadata.jsonl"

    prediction_rows: list[dict[str, Any]] = []
    metadata_rows: list[dict[str, Any]] = []
    total_cost = 0.0
    instance_count = 0
    question_count = 0

    for instance in _load_instances(config):
        instance_count += 1
        system = _make_system(config)
        try:
            results = _run_instance(system, instance)
        finally:
            system.close()

        cost = system.client.ledger.total_cost_usd
        total_cost += cost

        for question, answer in results:
            question_count += 1
            if dataset == "longmemeval":
                prediction_rows.append(
                    {
                        "question_id": question.question_id,
                        "hypothesis": answer.text,
                    }
                )
            else:
                split = config.get("split", "")
                source = str(instance.metadata.get("source", ""))
                prediction_rows.append(
                    {
                        "qa_pair_id": question.question_id,
                        "hypothesis": answer.text,
                        "gold_answers": question.gold_answers,
                        "source": source,
                        "split": split,
                    }
                )
            metadata_rows.append(
                {
                    "instance_id": instance.instance_id,
                    "question_id": question.question_id,
                    "system": system_name,
                    "dataset": dataset,
                    "usage": {
                        "prompt_tokens": answer.usage.prompt_tokens,
                        "completion_tokens": answer.usage.completion_tokens,
                    },
                    "latency_ms": answer.latency_ms,
                    "tool_calls": answer.tool_calls,
                    "truncated": answer.metadata.get("truncated", False),
                    "dropped_events": answer.metadata.get("dropped_events", 0),
                    "cost_usd": cost,
                }
            )

    if prediction_rows:
        with predictions_path.open("w", encoding="utf-8") as f:
            for row in prediction_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    _write_metadata(metadata_path, metadata_rows)

    summary = {
        "dataset": dataset,
        "system": system_name,
        "instances": instance_count,
        "questions": question_count,
        "total_cost_usd": total_cost,
        "predictions_path": str(predictions_path),
        "metadata_path": str(metadata_path),
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the droid-brain benchmark harness")
    parser.add_argument("--dataset", required=True, choices=["longmemeval", "mab"])
    parser.add_argument("--split", default=None, help="MAB split (Accurate_Retrieval or Conflict_Resolution)")
    parser.add_argument("--source-filter", default=None, help="Filter rows by source/question_type")
    parser.add_argument("--system", required=True, choices=list(SYSTEMS))
    parser.add_argument("--max-instances", type=int, default=None)
    parser.add_argument("--out", default=str(RESULTS_DIR))
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--model", default=AGENT_MODEL)
    parser.add_argument("--temperature", type=float, default=TEMPERATURE)
    args = parser.parse_args(argv)

    if args.system != "longctx":
        ensure_llm_credentials()
    # longctx also needs credentials, so always verify.
    ensure_llm_credentials()

    config = {
        "dataset": args.dataset,
        "split": args.split,
        "source_filter": args.source_filter,
        "system": args.system,
        "max_instances": args.max_instances,
        "out": args.out,
        "parallelism": args.parallelism,
        "model": args.model,
        "temperature": args.temperature,
    }

    summary = run(config)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
