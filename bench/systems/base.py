"""Common base class for benchmark memory systems."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

from bench.ir.types import EvidenceEvent, Question
from bench.llm.client import Usage


@dataclass
class Answer:
    """A system's answer to one question, plus diagnostic metadata."""

    text: str
    source_ids: list[str] = field(default_factory=list)
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source_ids": self.source_ids,
            "tool_calls": self.tool_calls,
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
            },
            "latency_ms": self.latency_ms,
            "metadata": self.metadata,
        }


class MemorySystem(ABC):
    """Abstract memory system: ingest events, answer questions."""

    @abstractmethod
    def ingest(
        self, events: Iterator[EvidenceEvent], *, question_timestamp: Optional[str] = None
    ) -> None:
        """Stream evidence events into the system in chronological order.

        When `question_timestamp` is provided, the system should ignore any
        evidence dated after that point (defense-in-depth for date-bounded
        benchmarks such as LongMemEval).
        """
        ...

    @abstractmethod
    def answer(self, question: Question) -> Answer:
        """Answer a question using the ingested evidence."""
        ...

    def close(self) -> None:
        """Optional cleanup hook (e.g., removing temp directories)."""
        pass
