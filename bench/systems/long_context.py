"""Long-context baseline: no memory, sliding-window evidence in the prompt."""

from __future__ import annotations

import time
from typing import Iterator

import tiktoken

from bench.config import AGENT_DEFAULT_MAX_TOKENS
from bench.ir.types import EvidenceEvent, Question
from bench.llm.client import ContentFilteredError, LLMClient, Usage
from bench.prompts.templates import long_context_system_prompt
from bench.systems.base import Answer, MemorySystem


# Budget for the model context window minus reserved space for system + answer.
_LONG_CONTEXT_BUDGET = 128_000 - 6_000


def _encoding_for_model(model: str) -> tiktoken.Encoding:
    """Return the tiktoken encoding for a model, falling back to o200k_base."""
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("o200k_base")


class LongContextBaseline(MemorySystem):
    """No memory; answer by putting as much evidence as fits in the prompt."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_budget: int = _LONG_CONTEXT_BUDGET,
        seed: int = 42,
    ):
        self.client = llm_client
        self.context_budget = context_budget
        self._seed = seed
        self._events: list[EvidenceEvent] = []
        self._ingest_dropped_after_date: int = 0
        self._last_question_timestamp: str | None = None
        self._tokenizer = _encoding_for_model(self.client.model)

    def ingest(
        self, events: Iterator[EvidenceEvent], *, question_timestamp: str | None = None
    ) -> None:
        self._last_question_timestamp = question_timestamp
        self._ingest_dropped_after_date = 0
        for event in events:
            if question_timestamp is not None and event.timestamp is not None:
                if event.timestamp > question_timestamp:
                    self._ingest_dropped_after_date += 1
                    continue
            self._events.append(event)

    def answer(self, question: Question) -> Answer:
        start = time.perf_counter()
        system_prompt = long_context_system_prompt(question.question_timestamp)
        question_prefix = f"Question: {question.text}\nAnswer directly based on the evidence above."
        evidence_text, included_events, truncation = self._build_evidence_text(
            system_prompt, question_prefix, question.question_timestamp
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": evidence_text + "\n\n" + question_prefix},
        ]
        try:
            response = self.client.chat(
                messages=messages,
                temperature=0,
                max_tokens=AGENT_DEFAULT_MAX_TOKENS,
                seed=self._seed,
            )
        except ContentFilteredError:
            latency_ms = (time.perf_counter() - start) * 1000
            return Answer(
                text="",
                source_ids=[],
                tool_calls=0,
                usage=Usage(),
                latency_ms=latency_ms,
                metadata={"content_filtered": True},
            )
        latency_ms = (time.perf_counter() - start) * 1000
        return Answer(
            text=response.content,
            source_ids=[e.source_id for e in included_events],
            tool_calls=0,
            usage=response.usage,
            latency_ms=latency_ms,
            metadata={
                "truncated": truncation.truncated,
                "dropped_events": truncation.dropped_events,
                "context_tokens": truncation.context_tokens,
                "included_events": len(included_events),
                "ingest_dropped_after_date": self._ingest_dropped_after_date,
            },
        )

    def _build_evidence_text(
        self,
        system_prompt: str,
        question_prefix: str,
        question_timestamp: str | None,
    ) -> tuple[str, list[EvidenceEvent], "Truncation"]:
        prefix = "Evidence (oldest first):\n\n"
        events = list(self._events)
        if question_timestamp:
            # Defensive filter for events after the question date.
            events = [e for e in events if (e.timestamp or "") <= question_timestamp]

        parts: list[str] = []
        included: list[EvidenceEvent] = []
        for i, event in enumerate(events):
            parts.append(
                f"[{i}] source_id={event.source_id} timestamp={event.timestamp}\n{event.text}"
            )
            included.append(event)

        full_text = prefix + "\n\n".join(parts)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": full_text + "\n\n" + question_prefix},
        ]
        total_tokens = self._count_messages(messages)

        if total_tokens <= self.context_budget:
            return full_text, included, Truncation(False, 0, total_tokens)

        # Drop oldest events first until the fully-rendered prompt fits.
        dropped = 0
        while parts and total_tokens > self.context_budget:
            parts.pop(0)
            included.pop(0)
            dropped += 1
            full_text = prefix + "\n\n".join(parts)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_text + "\n\n" + question_prefix},
            ]
            total_tokens = self._count_messages(messages)

        return full_text, included, Truncation(True, dropped, total_tokens)

    def _count_messages(self, messages: list[dict[str, str]]) -> int:
        """Approximate token count of a list of chat messages (content + per-message overhead)."""
        # Per-message overhead accounts for the role label and delimiter tokens.
        per_message_overhead = 4
        total = 0
        for m in messages:
            content = m.get("content", "")
            total += per_message_overhead
            # disallowed_special=(): haystack text can contain literal strings
            # like "<|endoftext|>" that tiktoken refuses to encode by default.
            total += len(self._tokenizer.encode(str(content), disallowed_special=()))
        return total


class Truncation:
    def __init__(self, truncated: bool, dropped_events: int, context_tokens: int):
        self.truncated = truncated
        self.dropped_events = dropped_events
        self.context_tokens = context_tokens
