"""Tests for the benchmark runner."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Iterator, Optional

import pytest

from bench.harness import runner
from bench.ir.types import BenchmarkInstance, EvidenceEvent, Question
from bench.llm.client import FakeLLMClient, Usage
from bench.systems.base import Answer, MemorySystem


class _FakeSystem(MemorySystem):
    def __init__(self, client: FakeLLMClient) -> None:
        self.client = client
        self.ingested: list[EvidenceEvent] = []

    def ingest(
        self, events: Iterator[EvidenceEvent], *, question_timestamp: Optional[str] = None
    ) -> None:
        self.ingested.extend(events)

    def answer(self, question: Question) -> Answer:
        self.client.ledger.add(Usage(10, 5), self.client.model)
        return Answer(
            text=f"answer for {question.question_id}",
            source_ids=[e.source_id for e in self.ingested[:1]],
            tool_calls=1,
            usage=Usage(10, 5),
            latency_ms=1.0,
        )


def test_runner_longmemeval_output_format(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client, k, seed: _FakeSystem(client)})
    monkeypatch.setattr(
        runner,
        "_load_instances",
        lambda cfg: [
            BenchmarkInstance(
                instance_id="lme_0",
                events=[EvidenceEvent(event_id="e1", source_id="s0", timestamp="2023-05-20", text="x")],
                questions=[Question(question_id="lme_0", text="q?")],
            )
        ],
    )

    config = {
        "dataset": "longmemeval",
        "system": "fake",
        "max_instances": 1,
        "out": str(tmp_path / "results"),
        "temperature": 0.0,
        "model": "gpt-4o-mini",
    }
    summary = runner.run(config)
    assert summary["questions"] == 1

    predictions_path = Path(summary["predictions_path"])
    metadata_path = Path(summary["metadata_path"])
    assert predictions_path.exists()
    assert metadata_path.exists()

    preds = [json.loads(line) for line in predictions_path.read_text().splitlines()]
    assert preds == [{"question_id": "lme_0", "hypothesis": "answer for lme_0"}]

    meta = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    assert meta[0]["question_id"] == "lme_0"
    assert meta[0]["tool_calls"] == 1
    assert meta[0]["usage"]["prompt_tokens"] == 10


def test_runner_mab_output_format(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client, k, seed: _FakeSystem(client)})
    monkeypatch.setattr(
        runner,
        "_load_instances",
        lambda cfg: [
            BenchmarkInstance(
                instance_id="ar_0",
                events=[EvidenceEvent(event_id="e1", source_id="s0", timestamp="0", text="x")],
                questions=[
                    Question(
                        question_id="ar_0_0",
                        text="What is A?",
                        gold_answers=["answer A"],
                        ability="longmemeval_s_0",
                    )
                ],
                metadata={"source": "longmemeval_s_0", "split": "Accurate_Retrieval"},
            )
        ],
    )

    config = {
        "dataset": "mab",
        "split": "Accurate_Retrieval",
        "system": "fake",
        "max_instances": 1,
        "out": str(tmp_path / "results"),
        "temperature": 0.0,
        "model": "gpt-4o-mini",
    }
    summary = runner.run(config)
    assert summary["questions"] == 1

    predictions_path = Path(summary["predictions_path"])
    preds = [json.loads(line) for line in predictions_path.read_text().splitlines()]
    assert preds[0]["qa_pair_id"] == "ar_0_0"
    assert "gold_answers" not in preds[0]
    assert preds[0]["instance_id"] == "ar_0"
    assert preds[0]["source"] == "longmemeval_s_0"
    assert preds[0]["split"] == "Accurate_Retrieval"


def test_runner_forwards_question_timestamp(tmp_path: Path, monkeypatch) -> None:
    received_timestamps: list[Optional[str]] = []

    class _TimestampCapturingSystem(_FakeSystem):
        def ingest(
            self, events: Iterator[EvidenceEvent], *, question_timestamp: Optional[str] = None
        ) -> None:
            received_timestamps.append(question_timestamp)
            super().ingest(events, question_timestamp=question_timestamp)

    monkeypatch.setattr(
        runner, "SYSTEMS", {"fake": lambda client, k, seed: _TimestampCapturingSystem(client)}
    )
    monkeypatch.setattr(
        runner,
        "_load_instances",
        lambda cfg: [
            BenchmarkInstance(
                instance_id="lme_0",
                events=[EvidenceEvent(event_id="e1", source_id="s0", timestamp="2023-05-20", text="x")],
                questions=[
                    Question(
                        question_id="lme_0",
                        text="q?",
                        question_timestamp="2023-05-21",
                    )
                ],
            )
        ],
    )

    config = {
        "dataset": "longmemeval",
        "system": "fake",
        "max_instances": 1,
        "out": str(tmp_path / "results"),
        "temperature": 0.0,
        "model": "gpt-4o-mini",
    }
    runner.run(config)
    assert received_timestamps == ["2023-05-21"]


def test_runner_mab_passes_none_question_timestamp(tmp_path: Path, monkeypatch) -> None:
    received_timestamps: list[Optional[str]] = []

    class _TimestampCapturingSystem(_FakeSystem):
        def ingest(
            self, events: Iterator[EvidenceEvent], *, question_timestamp: Optional[str] = None
        ) -> None:
            received_timestamps.append(question_timestamp)
            super().ingest(events, question_timestamp=question_timestamp)

    monkeypatch.setattr(
        runner, "SYSTEMS", {"fake": lambda client, k, seed: _TimestampCapturingSystem(client)}
    )
    monkeypatch.setattr(
        runner,
        "_load_instances",
        lambda cfg: [
            BenchmarkInstance(
                instance_id="ar_0",
                events=[EvidenceEvent(event_id="e1", source_id="s0", timestamp="0", text="x")],
                questions=[Question(question_id="ar_0_0", text="What is A?")],
                metadata={"source": "longmemeval_s_0", "split": "Accurate_Retrieval"},
            )
        ],
    )

    config = {
        "dataset": "mab",
        "split": "Accurate_Retrieval",
        "system": "fake",
        "max_instances": 1,
        "out": str(tmp_path / "results"),
        "temperature": 0.0,
        "model": "gpt-4o-mini",
    }
    runner.run(config)
    assert received_timestamps == [None]


def test_runner_per_instance_cost_delta(tmp_path: Path, monkeypatch) -> None:
    """Cost is recorded per-instance, not cumulative across instances."""
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client, k, seed: _FakeSystem(client)})
    monkeypatch.setattr(
        runner,
        "_load_instances",
        lambda cfg: [
            BenchmarkInstance(
                instance_id=f"lme_{i}",
                events=[EvidenceEvent(event_id=f"e{i}", source_id=f"s{i}", timestamp="2023-05-20", text="x")],
                questions=[Question(question_id=f"lme_{i}", text="q?")],
            )
            for i in range(3)
        ],
    )

    config = {
        "dataset": "longmemeval",
        "system": "fake",
        "max_instances": 3,
        "out": str(tmp_path / "results"),
        "temperature": 0.0,
        "model": "gpt-4o-mini",
    }
    summary = runner.run(config)
    metadata_path = Path(summary["metadata_path"])
    rows = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    costs = [round(row["cost_usd"], 8) for row in rows]
    assert len(set(costs)) == 1, f"Per-question costs should be equal, got {costs}"
    instance_costs = [round(row["instance_cost_usd"], 8) for row in rows]
    assert len(set(instance_costs)) == 1, f"Instance costs should be equal, got {instance_costs}"
    assert summary["total_cost_usd"] == pytest.approx(sum(row["instance_cost_usd"] for row in rows), rel=1e-6)


def test_runner_catches_instance_error_and_continues(tmp_path: Path, monkeypatch) -> None:
    class _FailingSystem(_FakeSystem):
        def answer(self, question: Question) -> Answer:
            raise RuntimeError("boom")

    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client, k, seed: _FailingSystem(client)})
    monkeypatch.setattr(
        runner,
        "_load_instances",
        lambda cfg: [
            BenchmarkInstance(
                instance_id="lme_0",
                events=[EvidenceEvent(event_id="e1", source_id="s0", timestamp="2023-05-20", text="x")],
                questions=[Question(question_id="lme_0", text="q?")],
            )
        ],
    )

    config = {
        "dataset": "longmemeval",
        "system": "fake",
        "max_instances": 1,
        "out": str(tmp_path / "results"),
        "temperature": 0.0,
        "model": "gpt-4o-mini",
    }
    summary = runner.run(config)
    assert summary["instances"] == 1
    assert summary["questions"] == 1
    assert summary["failures"] == 1

    predictions_path = Path(summary["predictions_path"])
    preds = [json.loads(line) for line in predictions_path.read_text().splitlines()]
    assert preds[0]["hypothesis"] == ""

    metadata_path = Path(summary["metadata_path"])
    rows = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    assert rows[0]["error"].startswith("RuntimeError")


def _two_instance_config(tmp_path: Path, **extra):
    instances = [
        BenchmarkInstance(
            instance_id=f"lme_{i}",
            events=[EvidenceEvent(event_id=f"e{i}", source_id=f"s{i}", timestamp="2023-05-20", text="x")],
            questions=[Question(question_id=f"lme_{i}", text="q?")],
        )
        for i in range(2)
    ]
    config = {
        "dataset": "longmemeval",
        "system": "fake",
        "instances": instances,
        "out": str(tmp_path / "results"),
        "temperature": 0.0,
        "model": "gpt-4o-mini",
    }
    config.update(extra)
    return config


def test_runner_fresh_run_truncates_stale_predictions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client, k, seed: _FakeSystem(client)})
    run_dir = tmp_path / "results" / "longmemeval" / "fake"
    run_dir.mkdir(parents=True)
    (run_dir / "predictions.jsonl").write_text(
        json.dumps({"question_id": "STALE", "hypothesis": "old"}) + "\n", encoding="utf-8"
    )
    (run_dir / "metadata.jsonl").write_text('{"stale": true}\n', encoding="utf-8")

    summary = runner.run(_two_instance_config(tmp_path))
    assert summary["questions"] == 2

    preds = [json.loads(line) for line in (run_dir / "predictions.jsonl").read_text().splitlines()]
    assert {p["question_id"] for p in preds} == {"lme_0", "lme_1"}
    assert all(p["question_id"] != "STALE" for p in preds)
    meta = [json.loads(line) for line in (run_dir / "metadata.jsonl").read_text().splitlines()]
    assert all("stale" not in m for m in meta)


def test_runner_resume_appends_and_skips_completed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client, k, seed: _FakeSystem(client)})
    run_dir = tmp_path / "results" / "longmemeval" / "fake"
    run_dir.mkdir(parents=True)
    # Simulate a crashed earlier worker: lme_0 already predicted, lme_1 not.
    (run_dir / "predictions.jsonl").write_text(
        json.dumps({"question_id": "lme_0", "hypothesis": "from crash"}) + "\n", encoding="utf-8"
    )

    summary = runner.run(_two_instance_config(tmp_path, resume=True))
    assert summary["questions"] == 1  # only lme_1 ran this time

    preds = [json.loads(line) for line in (run_dir / "predictions.jsonl").read_text().splitlines()]
    assert [p["question_id"] for p in preds] == ["lme_0", "lme_1"]
    assert preds[0]["hypothesis"] == "from crash"  # original row untouched
    assert preds[1]["hypothesis"] == "answer for lme_1"  # appended, not rewritten
