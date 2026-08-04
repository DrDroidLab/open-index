"""Plain IR types for benchmark datasets (no pydantic)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


@dataclass(frozen=True)
class EvidenceEvent:
    """One piece of evidence in a benchmark instance."""

    event_id: str
    source_id: str
    timestamp: Optional[str]  # ISO-like date or None when unavailable
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Question:
    """One question in a benchmark instance."""

    question_id: str
    text: str
    gold_answers: list[str] = field(default_factory=list)
    ability: str = ""  # verbatim or derived tag (e.g. question_type / source)
    gold_evidence_ids: list[str] = field(default_factory=list)
    question_timestamp: Optional[str] = None  # e.g. LongMemEval question_date
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkInstance:
    """A single evaluation instance: evidence stream + its questions."""

    instance_id: str
    events: list[EvidenceEvent] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def question_count(self) -> int:
        return len(self.questions)


# ---------------------------------------------------------------------------
# JSONL helpers
# ---------------------------------------------------------------------------


def _to_json(obj: Any) -> Any:
    """Recursively convert dataclasses to dicts."""
    if isinstance(obj, (EvidenceEvent, Question, BenchmarkInstance)):
        return asdict(obj)
    if isinstance(obj, list):
        return [_to_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_json(v) for k, v in obj.items()}
    return obj


def _from_json_event(data: dict[str, Any]) -> EvidenceEvent:
    return EvidenceEvent(
        event_id=data["event_id"],
        source_id=data["source_id"],
        timestamp=data.get("timestamp"),
        text=data["text"],
        metadata=data.get("metadata", {}),
    )


def _from_json_question(data: dict[str, Any]) -> Question:
    return Question(
        question_id=data["question_id"],
        text=data["text"],
        gold_answers=list(data.get("gold_answers", [])),
        ability=data.get("ability", ""),
        gold_evidence_ids=list(data.get("gold_evidence_ids", [])),
        question_timestamp=data.get("question_timestamp"),
        metadata=data.get("metadata", {}),
    )


def _from_json_instance(data: dict[str, Any]) -> BenchmarkInstance:
    return BenchmarkInstance(
        instance_id=data["instance_id"],
        events=[_from_json_event(e) for e in data.get("events", [])],
        questions=[_from_json_question(q) for q in data.get("questions", [])],
        metadata=data.get("metadata", {}),
    )


def event_to_json(event: EvidenceEvent) -> str:
    """Serialize one EvidenceEvent to a JSON line."""
    return json.dumps(_to_json(event), ensure_ascii=False, sort_keys=True)


def question_to_json(question: Question) -> str:
    """Serialize one Question to a JSON line."""
    return json.dumps(_to_json(question), ensure_ascii=False, sort_keys=True)


def instance_to_json(instance: BenchmarkInstance) -> str:
    """Serialize one BenchmarkInstance to a JSON line."""
    return json.dumps(_to_json(instance), ensure_ascii=False, sort_keys=True)


def event_from_json(line: str) -> EvidenceEvent:
    """Parse one EvidenceEvent from a JSON line."""
    return _from_json_event(json.loads(line))


def question_from_json(line: str) -> Question:
    """Parse one Question from a JSON line."""
    return _from_json_question(json.loads(line))


def instance_from_json(line: str) -> BenchmarkInstance:
    """Parse one BenchmarkInstance from a JSON line."""
    return _from_json_instance(json.loads(line))


def write_events(path: Path | str, events: Iterable[EvidenceEvent]) -> None:
    """Write EvidenceEvents to a JSONL file."""
    with Path(path).open("w", encoding="utf-8") as f:
        for event in events:
            f.write(event_to_json(event) + "\n")


def write_questions(path: Path | str, questions: Iterable[Question]) -> None:
    """Write Questions to a JSONL file."""
    with Path(path).open("w", encoding="utf-8") as f:
        for question in questions:
            f.write(question_to_json(question) + "\n")


def write_instances(path: Path | str, instances: Iterable[BenchmarkInstance]) -> None:
    """Write BenchmarkInstances to a JSONL file."""
    with Path(path).open("w", encoding="utf-8") as f:
        for instance in instances:
            f.write(instance_to_json(instance) + "\n")


def read_events(path: Path | str) -> Iterator[EvidenceEvent]:
    """Yield EvidenceEvents from a JSONL file."""
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield event_from_json(line)


def read_questions(path: Path | str) -> Iterator[Question]:
    """Yield Questions from a JSONL file."""
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield question_from_json(line)


def read_instances(path: Path | str) -> Iterator[BenchmarkInstance]:
    """Yield BenchmarkInstances from a JSONL file."""
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield instance_from_json(line)
