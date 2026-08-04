"""Tests for the sharded full-run wrapper."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from bench import run_full
from bench.harness import runner
from bench.ir.types import (
    BenchmarkInstance,
    EvidenceEvent,
    Question,
    read_instances,
    write_instances,
)
from bench.llm.client import FakeLLMClient, Usage
from bench.systems.base import Answer, MemorySystem


class _FakeSystem(MemorySystem):
    """Deterministic system that records cost per answer."""

    def __init__(self, client: FakeLLMClient) -> None:
        self.client = client

    def ingest(
        self, events: Iterator[EvidenceEvent], *, question_timestamp: Optional[str] = None
    ) -> None:
        pass

    def answer(self, question: Question) -> Answer:
        self.client.ledger.add(Usage(10, 5), self.client.model)
        return Answer(
            text=f"answer for {question.question_id}",
            source_ids=["s0"],
            tool_calls=1,
            usage=Usage(10, 5),
            latency_ms=1.0,
        )

    def close(self) -> None:
        pass


def _make_instances(n: int, prefix: str = "q") -> list[BenchmarkInstance]:
    return [
        BenchmarkInstance(
            instance_id=f"i_{i}",
            events=[EvidenceEvent(event_id=f"e{i}", source_id=f"s{i}", timestamp="0", text="x")],
            questions=[Question(question_id=f"{prefix}_{i}", text="x")],
        )
        for i in range(n)
    ]


def test_shard_instances_complete_disjoint_and_deterministic() -> None:
    instances = _make_instances(10)
    shards = run_full.shard_instances(instances, 3)

    assert len(shards) == 3

    all_qids = [q.question_id for shard in shards for inst in shard for q in inst.questions]
    assert sorted(all_qids) == sorted([f"q_{i}" for i in range(10)])

    seen: set[str] = set()
    for shard in shards:
        for inst in shard:
            for q in inst.questions:
                assert q.question_id not in seen
                seen.add(q.question_id)

    # Deterministic: same input yields same shard layout.
    assert run_full.shard_instances(instances, 3) == shards


def test_shard_instances_single_worker() -> None:
    instances = _make_instances(5)
    shards = run_full.shard_instances(instances, 1)
    assert len(shards) == 1
    assert [q.question_id for inst in shards[0] for q in inst.questions] == [f"q_{i}" for i in range(5)]


def test_worker_skips_existing_predictions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client, k, seed: _FakeSystem(client)})

    out_dir = tmp_path / "shard_0"
    out_dir.mkdir()

    # Existing completed work: one question already predicted.
    (out_dir / "predictions.jsonl").write_text(
        json.dumps({"question_id": "q_0", "hypothesis": "old"}) + "\n"
    )
    (out_dir / "metadata.jsonl").write_text(
        json.dumps(
            {
                "question_id": "q_0",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "latency_ms": 1.0,
                "tool_calls": 1,
                "retrieved_source_ids": ["s0"],
                "truncated": False,
                "dropped_events": 0,
                "cost_usd": 0.01,
                "instance_cost_usd": 0.01,
                "error": None,
            }
        )
        + "\n"
    )
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "dataset": "longmemeval",
                "system": "fake",
                "instances": 1,
                "questions": 1,
                "failures": 0,
                "total_cost_usd": 0.01,
                "k": 5,
                "seed": 42,
            }
        )
    )

    instances = [
        BenchmarkInstance(
            instance_id="i_0",
            events=[],
            questions=[Question(question_id="q_0", text="x")],
        ),
        BenchmarkInstance(
            instance_id="i_1",
            events=[],
            questions=[Question(question_id="q_1", text="x")],
        ),
    ]
    instances_path = tmp_path / "instances.jsonl"
    write_instances(instances_path, instances)

    config = {
        "dataset": "longmemeval",
        "system": "fake",
        "out_dir": str(out_dir),
        "instances_path": str(instances_path),
        "k": 5,
        "seed": 42,
        "model": "gpt-4o-mini",
        "temperature": 0.0,
    }
    exit_code = run_full._worker_entry(config)
    assert exit_code == 0

    preds = [json.loads(line) for line in (out_dir / "predictions.jsonl").read_text().splitlines()]
    assert len(preds) == 2
    qids = {p["question_id"] for p in preds}
    assert qids == {"q_0", "q_1"}

    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["questions"] == 2
    assert summary["instances"] == 2
    assert summary["total_cost_usd"] > 0.01


def test_worker_aborts_on_cost_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client, k, seed: _FakeSystem(client)})

    out_dir = tmp_path / "shard_0"
    out_dir.mkdir()

    instances = _make_instances(3)
    instances_path = tmp_path / "instances.jsonl"
    write_instances(instances_path, instances)

    config = {
        "dataset": "longmemeval",
        "system": "fake",
        "out_dir": str(out_dir),
        "instances_path": str(instances_path),
        "k": 5,
        "seed": 42,
        "model": "gpt-4o-mini",
        "temperature": 0.0,
        "cost_cap": 1e-6,  # lower than the cost of a single question.
    }
    exit_code = run_full._worker_entry(config)
    assert exit_code == 0

    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["questions"] < 3
    assert summary.get("cost_cap_reached") is True
    assert summary["cost_cap_usd"] == 1e-6


def test_merge_shard_summaries(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    for i in range(2):
        shard_dir = out_dir / f"shard_{i}"
        shard_dir.mkdir()
        (shard_dir / "predictions.jsonl").write_text(
            json.dumps({"question_id": f"q_{i}", "hypothesis": "x"}) + "\n"
        )
        (shard_dir / "metadata.jsonl").write_text(
            json.dumps(
                {
                    "question_id": f"q_{i}",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "latency_ms": 1.0,
                    "tool_calls": 1,
                    "cost_usd": 0.01,
                    "instance_cost_usd": 0.01,
                }
            )
            + "\n"
        )
        (shard_dir / "summary.json").write_text(
            json.dumps(
                {
                    "dataset": "longmemeval",
                    "system": "flat",
                    "instances": 1,
                    "questions": 1,
                    "failures": 0,
                    "total_cost_usd": 0.01,
                    "k": 5,
                    "seed": 42,
                }
            )
        )

    merged = run_full.merge_shards(
        out_dir,
        dataset="longmemeval",
        system="flat",
        split="",
        k=5,
        seed=42,
    )
    assert merged["instances"] == 2
    assert merged["questions"] == 2
    assert merged["failures"] == 0
    assert merged["total_cost_usd"] == 0.02
    assert merged["dataset"] == "longmemeval"
    assert merged["system"] == "flat"
    assert merged["k"] == 5

    merged_dir = out_dir / "merged"
    preds = [json.loads(line) for line in (merged_dir / "predictions.jsonl").read_text().splitlines()]
    assert len(preds) == 2
    meta = [json.loads(line) for line in (merged_dir / "metadata.jsonl").read_text().splitlines()]
    assert len(meta) == 2


def test_parse_args_orchestrator() -> None:
    args = run_full.parse_args(
        [
            "--dataset",
            "longmemeval",
            "--system",
            "structured",
            "--workers",
            "4",
            "--max-instances",
            "100",
            "--max-cost",
            "50.0",
            "--no-score",
        ]
    )
    assert args.dataset == "longmemeval"
    assert args.system == "structured"
    assert args.workers == 4
    assert args.max_instances == 100
    assert args.max_cost == 50.0
    assert args.no_score is True
    assert args.worker is False


def test_parse_args_mab_requires_split() -> None:
    with pytest.raises(SystemExit):
        run_full.parse_args(["--dataset", "mab", "--system", "flat"])


def test_parse_args_worker_does_not_require_dataset() -> None:
    args = run_full.parse_args(["--worker", "--config-path", "/tmp/config.json"])
    assert args.worker is True
    assert args.config_path == "/tmp/config.json"


def test_worker_cli_entry(tmp_path: Path, monkeypatch) -> None:
    """The --worker CLI path reads a config file and runs the shard."""
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client, k, seed: _FakeSystem(client)})

    out_dir = tmp_path / "shard"
    out_dir.mkdir()
    instances_path = out_dir / "instances.jsonl"
    write_instances(instances_path, _make_instances(2))

    config = {
        "dataset": "longmemeval",
        "system": "fake",
        "out_dir": str(out_dir),
        "instances_path": str(instances_path),
        "k": 5,
        "seed": 42,
        "model": "gpt-4o-mini",
        "temperature": 0.0,
    }
    config_path = out_dir / "worker_config.json"
    config_path.write_text(json.dumps(config))

    exit_code = run_full.main(["--worker", "--config-path", str(config_path)])
    assert exit_code == 0

    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["questions"] == 2
    assert summary["instances"] == 2


def test_build_worker_configs(tmp_path: Path) -> None:
    instances = _make_instances(4)
    args = run_full.parse_args(
        [
            "--dataset",
            "longmemeval",
            "--system",
            "flat",
            "--workers",
            "2",
            "--max-cost",
            "10.0",
        ]
    )
    shards = run_full.shard_instances(instances, args.workers)
    configs = run_full._build_worker_configs(args, shards, tmp_path)
    assert len(configs) == 2
    assert configs[0]["cost_cap"] == 5.0
    assert configs[1]["cost_cap"] == 5.0
    assert Path(configs[0]["instances_path"]).exists()
    assert Path(configs[1]["instances_path"]).exists()


def test_run_full_orchestration_with_fake_workers(tmp_path: Path, monkeypatch) -> None:
    """End-to-end orchestration test with mocked subprocess workers."""
    instances = _make_instances(4)
    monkeypatch.setattr(run_full, "load_instances", lambda args: instances)
    monkeypatch.setattr(run_full, "ensure_llm_credentials", lambda: None)

    class FakePopen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            self.cmd = cmd
            self.returncode = None
            self._config_path = cmd[cmd.index("--config-path") + 1]

        def wait(self, timeout: float | None = None) -> int:
            config = json.loads(Path(self._config_path).read_text())
            out_dir = Path(config["out_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            shard_instances = list(read_instances(config["instances_path"]))
            with (out_dir / "predictions.jsonl").open("w", encoding="utf-8") as f:
                for inst in shard_instances:
                    for q in inst.questions:
                        f.write(json.dumps({"question_id": q.question_id, "hypothesis": "x"}) + "\n")
            with (out_dir / "metadata.jsonl").open("w", encoding="utf-8") as f:
                for inst in shard_instances:
                    for q in inst.questions:
                        f.write(
                            json.dumps(
                                {
                                    "question_id": q.question_id,
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                                    "cost_usd": 0.01,
                                    "instance_cost_usd": 0.01,
                                }
                            )
                            + "\n"
                        )
            (out_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "dataset": config["dataset"],
                        "system": config["system"],
                        "instances": len(shard_instances),
                        "questions": sum(len(inst.questions) for inst in shard_instances),
                        "failures": 0,
                        "total_cost_usd": len(shard_instances) * 0.01,
                        "k": config["k"],
                        "seed": config["seed"],
                    }
                )
            )
            self.returncode = 0
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    out_dir = tmp_path / "out"
    exit_code = run_full.run_full(
        [
            "--dataset",
            "longmemeval",
            "--system",
            "flat",
            "--workers",
            "2",
            "--out",
            str(out_dir),
            "--no-score",
        ]
    )
    assert exit_code == 0

    assert (out_dir / "shard_0").is_dir()
    assert (out_dir / "shard_1").is_dir()
    merged_dir = out_dir / "merged"
    assert merged_dir.is_dir()
    preds = [json.loads(line) for line in (merged_dir / "predictions.jsonl").read_text().splitlines()]
    assert len(preds) == 4
    assert sorted(p["question_id"] for p in preds) == ["q_0", "q_1", "q_2", "q_3"]
    merged_summary = json.loads((merged_dir / "summary.json").read_text())
    assert merged_summary["questions"] == 4
    assert merged_summary["instances"] == 4
    assert merged_summary["total_cost_usd"] == 0.04


def test_worker_subprocess_runs_empty_shard(tmp_path: Path) -> None:
    """A real worker subprocess can handle an empty shard without LLM calls."""
    out_dir = tmp_path / "shard"
    out_dir.mkdir()
    instances_path = out_dir / "instances.jsonl"
    write_instances(instances_path, [])

    config = {
        "dataset": "longmemeval",
        "system": "flat",
        "out_dir": str(out_dir),
        "instances_path": str(instances_path),
        "k": 5,
        "seed": 42,
        "model": "gpt-4o-mini",
        "temperature": 0.0,
    }
    config_path = out_dir / "worker_config.json"
    config_path.write_text(json.dumps(config))

    proc = subprocess.Popen(
        [sys.executable, "-m", "bench.run_full", "--worker", "--config-path", str(config_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, f"Worker failed: {stderr}"

    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["questions"] == 0
    assert summary["instances"] == 0


def test_merge_concatenates_shard_probe_results(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for i in range(2):
        shard_dir = out_dir / f"shard_{i}"
        shard_dir.mkdir()
        (shard_dir / "predictions.jsonl").write_text(
            json.dumps({"question_id": f"q_{i}", "hypothesis": "x"}) + "\n"
        )
        (shard_dir / "metadata.jsonl").write_text("{}\n")
        (shard_dir / "summary.json").write_text(
            json.dumps({"instances": 1, "questions": 1, "failures": 0, "total_cost_usd": 0.01})
        )
        if i == 0:
            (shard_dir / "probe_results.json").write_text(
                json.dumps([{"question_id": "q_0", "correct": True}])
            )

    merged = run_full.merge_shards(out_dir, "longmemeval", "flat", "", 5, 42)

    probe_path = out_dir / "merged" / "probe_results.json"
    assert probe_path.exists()
    assert json.loads(probe_path.read_text()) == [{"question_id": "q_0", "correct": True}]
    assert merged["probe_results_path"] == str(probe_path)


def test_merge_without_probes_leaves_probe_path_none(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    shard_dir = out_dir / "shard_0"
    shard_dir.mkdir(parents=True)
    (shard_dir / "predictions.jsonl").write_text(
        json.dumps({"question_id": "q_0", "hypothesis": "x"}) + "\n"
    )
    (shard_dir / "metadata.jsonl").write_text("{}\n")
    (shard_dir / "summary.json").write_text(
        json.dumps({"instances": 1, "questions": 1, "failures": 0, "total_cost_usd": 0.01})
    )

    merged = run_full.merge_shards(out_dir, "longmemeval", "flat", "", 5, 42)

    assert merged["probe_results_path"] is None
    assert not (out_dir / "merged" / "probe_results.json").exists()


def test_merge_dedupes_duplicate_question_ids(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for i in range(2):
        shard_dir = out_dir / f"shard_{i}"
        shard_dir.mkdir()
        # Both shards contain a row for the same question (cross-shard re-run).
        (shard_dir / "predictions.jsonl").write_text(
            json.dumps({"question_id": "q_dup", "hypothesis": f"from_shard_{i}"}) + "\n"
        )
        (shard_dir / "metadata.jsonl").write_text("{}\n")
        (shard_dir / "summary.json").write_text(
            json.dumps({"instances": 1, "questions": 1, "failures": 0, "total_cost_usd": 0.01})
        )

    merged = run_full.merge_shards(out_dir, "longmemeval", "flat", "", 5, 42)

    preds = [json.loads(l) for l in (out_dir / "merged" / "predictions.jsonl").read_text().splitlines()]
    assert len(preds) == 1
    assert preds[0]["hypothesis"] == "from_shard_0"  # first occurrence wins
    assert merged["questions"] == 1
