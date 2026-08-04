"""Tests for the benchmark runner."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Iterator

from bench.harness import runner
from bench.ir.types import BenchmarkInstance, EvidenceEvent, Question
from bench.llm.client import FakeLLMClient, Usage
from bench.systems.base import Answer, MemorySystem


class _FakeSystem(MemorySystem):
    def __init__(self, client: FakeLLMClient) -> None:
        self.client = client
        self.ingested: list[EvidenceEvent] = []

    def ingest(self, events: Iterator[EvidenceEvent]) -> None:
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
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client: _FakeSystem(client)})
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
    monkeypatch.setattr(runner, "SYSTEMS", {"fake": lambda client: _FakeSystem(client)})
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
    assert preds[0]["gold_answers"] == ["answer A"]
    assert "source" in preds[0]
