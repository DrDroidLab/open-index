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
from bench.harness.probes import UpdateProbe, _is_update_question, extract_probes, probe_flat, probe_long_context, probe_structured
from bench.ir.types import BenchmarkInstance, Question, read_instances
from bench.llm.client import LLMClient, Usage
from bench.systems import FlatMemoryBaseline, LongContextBaseline, StructuredBrainMemory
from bench.systems.base import Answer, MemorySystem


SYSTEMS: dict[str, Callable[[LLMClient, int, int], MemorySystem]] = {
    "structured": lambda client, k, seed: StructuredBrainMemory(client, k=k, seed=seed),
    "flat": lambda client, k, seed: FlatMemoryBaseline(client, k=k, seed=seed),
    "longctx": lambda client, k, seed: LongContextBaseline(client, seed=seed),
}


def _instance_question_timestamp(instance: BenchmarkInstance) -> Optional[str]:
    """Return the bounding question timestamp for an instance, or None for MAB."""
    timestamps = [q.question_timestamp for q in instance.questions if q.question_timestamp is not None]
    return max(timestamps) if timestamps else None


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


def _load_instances_from_config(config: dict[str, Any]) -> list[BenchmarkInstance]:
    """Load instances from a config value: path, list, or via adapters."""
    raw = config.get("instances")
    if raw is None or raw == "":
        return list(_load_instances(config))
    if isinstance(raw, (str, Path)):
        return list(read_instances(Path(raw)))
    if isinstance(raw, list):
        return list(raw)
    # Treat any iterable as a list.
    return list(raw)


def _existing_qids_from_predictions(path: Path) -> set[str]:
    """Return question IDs already present in a predictions.jsonl file."""
    qids: set[str] = set()
    if not path.exists():
        return qids
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        for key in ("question_id", "qa_pair_id"):
            value = row.get(key)
            if value:
                qids.add(str(value))
    return qids


def _load_existing_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _load_existing_probe_results(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _make_system(
    config: dict[str, Any], client: LLMClient | None = None, k: int = 5, seed: int = 42
) -> MemorySystem:
    system_name = config["system"]
    factory = SYSTEMS.get(system_name)
    if factory is None:
        raise ValueError(f"Unknown system: {system_name}")
    if client is None:
        client = LLMClient(
            model=config.get("model", AGENT_MODEL),
            temperature=config.get("temperature", TEMPERATURE),
        )
    return factory(client, k, seed)


def _run_instance(
    system: MemorySystem, instance: BenchmarkInstance, question_timestamp: Optional[str] = None
) -> list[tuple[Question, Answer]]:
    system.ingest(iter(instance.events), question_timestamp=question_timestamp)
    results: list[tuple[Question, Answer]] = []
    for question in instance.questions:
        answer = system.answer(question)
        results.append((question, answer))
    return results


def _write_metadata(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _run_probes(
    system: MemorySystem,
    instance: BenchmarkInstance,
    client: LLMClient,
) -> list[dict[str, Any]]:
    """Run update probes against the system for this instance."""
    if not any(_is_update_question(q, instance.metadata) for q in instance.questions):
        return []

    probes = extract_probes([instance], client)
    results: list[dict[str, Any]] = []
    for probe in probes:
        if isinstance(system, StructuredBrainMemory):
            correct = probe_structured(system.brain, probe)
        elif isinstance(system, FlatMemoryBaseline):
            correct = probe_flat(system.brain, probe, client)
        elif isinstance(system, LongContextBaseline):
            # system._events is already filtered to the instance's question_timestamp by ingest.
            correct = probe_long_context(system._events, probe, client)
        else:
            continue
        results.append(
            {
                "question_id": probe.question_id,
                "subject": probe.subject,
                "attribute": probe.attribute,
                "expected_value": probe.expected_value,
                "correct": correct,
            }
        )
    return results


def _share_usage(usage: Usage, n: int) -> dict[str, float]:
    """Divide a Usage object evenly across n questions."""
    if n <= 0:
        return {"prompt_tokens": 0.0, "completion_tokens": 0.0}
    return {
        "prompt_tokens": usage.prompt_tokens / n,
        "completion_tokens": usage.completion_tokens / n,
    }


def run(config: dict[str, Any]) -> dict[str, Any]:
    """Run one dataset x system combination and write result files."""
    dataset = config["dataset"]
    system_name = config["system"]
    if "out_dir" in config:
        out_dir = Path(config["out_dir"])
    else:
        out_dir = Path(config.get("out", RESULTS_DIR)) / dataset / system_name
    out_dir.mkdir(parents=True, exist_ok=True)

    predictions_path = out_dir / "predictions.jsonl"
    metadata_path = out_dir / "metadata.jsonl"
    probe_results_path = out_dir / "probe_results.json"
    summary_path = out_dir / "summary.json"

    k = int(config.get("k", 5))
    seed = int(config.get("seed", 42))
    split = config.get("split", "")

    resume = bool(config.get("resume", False))
    cost_cap = max(0.0, float(config.get("cost_cap", 0.0) or 0.0))

    existing_qids: set[str] = set()
    existing_summary: dict[str, Any] = {}
    existing_probes: list[dict[str, Any]] = []
    if resume:
        existing_qids = _existing_qids_from_predictions(predictions_path)
        existing_summary = _load_existing_summary(summary_path)
        existing_probes = _load_existing_probe_results(probe_results_path)

    existing_instances = int(existing_summary.get("instances", 0))
    existing_questions = int(existing_summary.get("questions", 0))
    existing_failures = int(existing_summary.get("failures", 0))
    existing_cost = float(existing_summary.get("total_cost_usd", 0.0))

    prediction_rows: list[dict[str, Any]] = []
    metadata_rows: list[dict[str, Any]] = []
    if not resume:
        # Fresh run: truncate so the per-instance appends below cannot
        # duplicate stale rows from an earlier run in the same directory.
        predictions_path.write_text("", encoding="utf-8")
        metadata_path.write_text("", encoding="utf-8")

    def _flush_rows() -> None:
        """Append buffered rows to disk and clear the buffers.

        Flushed after every instance so a crashed worker never loses
        completed work and --resume can skip it on restart.
        """
        if prediction_rows:
            with predictions_path.open("a", encoding="utf-8") as f:
                for row in prediction_rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            prediction_rows.clear()
        if metadata_rows:
            with metadata_path.open("a", encoding="utf-8") as f:
                for row in metadata_rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            metadata_rows.clear()

    probe_results = list(existing_probes) if resume else []
    total_cost = 0.0
    instance_count = 0
    question_count = 0
    failure_count = 0
    cost_cap_reached = False

    instances = _load_instances_from_config(config)
    for instance in instances:
        if resume and any(q.question_id in existing_qids for q in instance.questions):
            continue
        if cost_cap > 0 and existing_cost + total_cost >= cost_cap:
            cost_cap_reached = True
            break

        instance_count += 1
        system = _make_system(config, client=config.get("client"), k=k, seed=seed)
        cost_before = system.client.ledger.total_cost_usd
        usage_before = system.client.ledger.usage()
        instance_error: str | None = None
        instance_results: list[tuple[Question, Answer]] = []

        try:
            results = _run_instance(
                system,
                instance,
                question_timestamp=_instance_question_timestamp(instance),
            )
            probe_results.extend(_run_probes(system, instance, system.client))
            instance_results = results
        except Exception as exc:
            instance_error = f"{type(exc).__name__}: {exc}"
            failure_count += 1
            for question in instance.questions:
                instance_results.append(
                    (
                        question,
                        Answer(
                            text="",
                            source_ids=[],
                            tool_calls=0,
                            usage=Usage(),
                            latency_ms=0.0,
                            metadata={"error": instance_error},
                        ),
                    )
                )
        finally:
            system.close()

        cost_after = system.client.ledger.total_cost_usd
        usage_after = system.client.ledger.usage()
        delta_cost = cost_after - cost_before
        delta_usage = Usage(
            prompt_tokens=usage_after.prompt_tokens - usage_before.prompt_tokens,
            completion_tokens=usage_after.completion_tokens - usage_before.completion_tokens,
        )
        total_cost += delta_cost

        num_questions = max(len(instance.questions), 1)
        shared_usage = _share_usage(delta_usage, num_questions)
        per_question_cost = delta_cost / num_questions

        for question, answer in instance_results:
            question_count += 1
            if dataset == "longmemeval":
                prediction_rows.append(
                    {
                        "question_id": question.question_id,
                        "hypothesis": answer.text,
                    }
                )
            else:
                source = str(instance.metadata.get("source", ""))
                prediction_rows.append(
                    {
                        "qa_pair_id": question.question_id,
                        "hypothesis": answer.text,
                        "source": source,
                        "split": split,
                        "instance_id": instance.instance_id,
                    }
                )
            metadata_rows.append(
                {
                    "instance_id": instance.instance_id,
                    "question_id": question.question_id,
                    "system": system_name,
                    "dataset": dataset,
                    "usage": shared_usage,
                    "instance_usage": {
                        "prompt_tokens": delta_usage.prompt_tokens,
                        "completion_tokens": delta_usage.completion_tokens,
                    },
                    "latency_ms": answer.latency_ms,
                    "tool_calls": answer.tool_calls,
                    "retrieved_source_ids": answer.source_ids,
                    "truncated": answer.metadata.get("truncated", False),
                    "content_filtered": answer.metadata.get("content_filtered", False),
                    "ingest_content_filtered": answer.metadata.get("ingest_content_filtered", 0),
                    "dropped_events": answer.metadata.get("dropped_events", 0),
                    "cost_usd": per_question_cost,
                    "instance_cost_usd": delta_cost,
                    "error": instance_error,
                }
            )

        _flush_rows()

    _flush_rows()

    if probe_results:
        probe_results_path.write_text(
            json.dumps(probe_results, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    total_instances = existing_instances + instance_count
    total_questions = existing_questions + question_count
    total_failures = existing_failures + failure_count
    total_cost_all = existing_cost + total_cost

    summary: dict[str, Any] = {
        "dataset": dataset,
        "system": system_name,
        "split": split,
        "instances": total_instances,
        "questions": total_questions,
        "failures": total_failures,
        "k": k,
        "seed": seed,
        "total_cost_usd": total_cost_all,
        "predictions_path": str(predictions_path),
        "metadata_path": str(metadata_path),
        "probe_results_path": str(probe_results_path) if probe_results else None,
    }
    if cost_cap_reached:
        summary["cost_cap_reached"] = True
        summary["cost_cap_usd"] = cost_cap
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
    parser.add_argument("--k", type=int, default=5, help="Retrieval budget (default 5)")
    parser.add_argument("--seed", type=int, default=42, help="LLM seed for deterministic tool loops")
    parser.add_argument("--model", default=AGENT_MODEL)
    parser.add_argument("--temperature", type=float, default=TEMPERATURE)
    args = parser.parse_args(argv)

    ensure_llm_credentials()

    config = {
        "dataset": args.dataset,
        "split": args.split,
        "source_filter": args.source_filter,
        "system": args.system,
        "max_instances": args.max_instances,
        "out": args.out,
        "k": args.k,
        "seed": args.seed,
        "model": args.model,
        "temperature": args.temperature,
    }

    summary = run(config)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
