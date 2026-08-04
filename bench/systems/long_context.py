"""Long-context baseline: no memory, sliding-window evidence in the prompt."""

from __future__ import annotations

import time
from typing import Iterator

import tiktoken

from bench.config import AGENT_DEFAULT_MAX_TOKENS
from bench.ir.types import EvidenceEvent, Question
from bench.llm.client import LLMClient, Usage
from bench.prompts.templates import long_context_system_prompt
from bench.systems.base import Answer, MemorySystem


# Budget for the model context window minus reserved space for system + answer.
_LONG_CONTEXT_BUDGET = 128_000 - 6_000


class LongContextBaseline(MemorySystem):
    """No memory; answer by putting as much evidence as fits in the prompt."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_budget: int = _LONG_CONTEXT_BUDGET,
        encoding: str = "cl100k_base",
    ):
        self.client = llm_client
        self.context_budget = context_budget
        self.encoding = encoding
        self._events: list[EvidenceEvent] = []
        self._tokenizer = tiktoken.get_encoding(encoding)

    def ingest(self, events: Iterator[EvidenceEvent]) -> None:
        self._events.extend(events)

    def answer(self, question: Question) -> Answer:
        start = time.perf_counter()
        system_prompt = long_context_system_prompt(question.question_timestamp)
        question_text = f"Question: {question.text}\nAnswer directly based on the evidence above."
        evidence_text, truncation = self._build_evidence_text(question)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": evidence_text + "\n\n" + question_text},
        ]
        response = self.client.chat(
            messages=messages,
            temperature=0,
            max_tokens=AGENT_DEFAULT_MAX_TOKENS,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        return Answer(
            text=response.content,
            source_ids=[],
            tool_calls=0,
            usage=response.usage,
            latency_ms=latency_ms,
            metadata={
                "truncated": truncation.truncated,
                "dropped_events": truncation.dropped_events,
                "context_tokens": truncation.context_tokens,
            },
        )

    def _build_evidence_text(self, question: Question) -> tuple[str, "Truncation"]:
        prefix = "Evidence (oldest first):\n\n"
        events = self._events
        if question.question_timestamp:
            # LongMemEval guarantees all haystacks precede the question date, but
            # enforce the invariant defensively.
            events = [e for e in events if (e.timestamp or "") <= question.question_timestamp]

        parts: list[str] = []
        for i, event in enumerate(events):
            parts.append(
                f"[{i}] source_id={event.source_id} timestamp={event.timestamp}\n{event.text}"
            )
        full_text = prefix + "\n\n".join(parts)
        total_tokens = self._count_tokens(full_text + "\n\n" + question.text)

        if total_tokens <= self.context_budget:
            return full_text, Truncation(False, 0, total_tokens)

        # Drop oldest events first until the prompt fits.
        dropped = 0
        while parts and total_tokens > self.context_budget:
            parts.pop(0)
            dropped += 1
            full_text = prefix + "\n\n".join(parts)
            total_tokens = self._count_tokens(full_text + "\n\n" + question.text)

        return full_text, Truncation(True, dropped, total_tokens)

    def _count_tokens(self, text: str) -> int:
        return len(self._tokenizer.encode(text))


class Truncation:
    def __init__(self, truncated: bool, dropped_events: int, context_tokens: int):
        self.truncated = truncated
        self.dropped_events = dropped_events
        self.context_tokens = context_tokens
