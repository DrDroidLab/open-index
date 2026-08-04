"""Shared base class for brain-backed benchmark memory systems."""

from __future__ import annotations

import shutil
import time
from abc import abstractmethod
from typing import Any, Callable, Iterator

from droid_brain.brain import Brain
from droid_brain.models import Entity

from bench.ir.types import EvidenceEvent, Question
from bench.prompts import answer_system_prompt, ingest_system_prompt
from bench.systems._tool_loop import answer_tool_handler, run_tool_loop
from bench.systems._utils import make_temp_brain, record_source_id
from bench.systems.base import Answer, MemorySystem


class BrainBackedMemorySystem(MemorySystem):
    """Common machinery for the structured and flat memory arms.

    Subclasses only need to provide the tool schemas and handlers for their
    specific doc_types; the temp-brain lifecycle, ingest/answer loops, and
    answer construction are shared here.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        config_name: str,
        prompt_variant: str,
        keep_state: bool = False,
        max_ingest_tools: int = 8,
        max_answer_tools: int = 12,
    ):
        self.client = llm_client
        self.keep_state = keep_state
        self.max_ingest_tools = max_ingest_tools
        self.max_answer_tools = max_answer_tools
        self._prompt_variant = prompt_variant
        self._brain_dir = make_temp_brain(config_name)
        self.brain = Brain.open(self._brain_dir)
        self._current_event: EvidenceEvent | None = None

    @property
    def _current_source_id(self) -> str:
        return self._current_event.source_id if self._current_event else ""

    @property
    def _current_timestamp(self) -> str:
        return self._current_event.timestamp if self._current_event else ""

    def _put_entity(
        self,
        entity_id: str,
        doc_type: str,
        name: str,
        fields: dict[str, Any],
    ) -> str:
        """Write an entity to the brain, tagging it with the current event source/timestamp."""
        self.brain.put_entity(
            Entity(
                id=entity_id,
                doc_type=doc_type,
                name=name,
                fields={
                    **fields,
                    "source_id": self._current_source_id,
                    "timestamp": self._current_timestamp,
                },
            )
        )
        return f"Wrote {entity_id}."

    def _format_search_result_lines(
        self,
        results: Any,
        retrieved: list[str],
        format_fn: Callable[[Entity], str],
    ) -> list[str]:
        """Format search results while recording retrieved source_ids."""
        lines: list[str] = []
        for r in results.results:
            raw = r.get("entity")
            if raw is None:
                continue
            entity = Entity.from_dict(raw)
            record_source_id(entity, retrieved)
            lines.append(format_fn(entity))
        return lines

    def ingest(self, events: Iterator[EvidenceEvent]) -> None:
        for event in events:
            self._current_event = event
            messages = [
                {"role": "system", "content": ingest_system_prompt(self._prompt_variant)},
                {
                    "role": "user",
                    "content": (
                        f"Event source_id={event.source_id} timestamp={event.timestamp}\n\n"
                        f"{event.text}"
                    ),
                },
            ]
            run_tool_loop(
                client=self.client,
                messages=messages,
                tools=self._ingest_tools(),
                handlers=self._ingest_handlers(),
                finish_tool="done",
                max_tool_calls=self.max_ingest_tools,
            )

    def answer(self, question: Question) -> Answer:
        start = time.perf_counter()
        retrieved_source_ids: list[str] = []
        system_prompt = answer_system_prompt(self._prompt_variant, question.question_timestamp)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if self._prompt_variant == "structured":
            messages.append(
                {"role": "system", "content": f"Brain navigation guide:\n{self.brain.navigation_guidelines()}"}
            )
        messages.append({"role": "user", "content": question.text})

        result = run_tool_loop(
            client=self.client,
            messages=messages,
            tools=self._answer_tools(),
            handlers=self._answer_handlers(retrieved_source_ids),
            finish_tool="answer",
            max_tool_calls=self.max_answer_tools,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        all_source_ids = list(dict.fromkeys(result.source_ids + retrieved_source_ids))
        return Answer(
            text=result.content,
            source_ids=all_source_ids,
            tool_calls=result.tool_calls,
            usage=result.usage,
            latency_ms=latency_ms,
            metadata=result.metadata,
        )

    def close(self) -> None:
        if not self.keep_state and self._brain_dir.exists():
            shutil.rmtree(self._brain_dir, ignore_errors=True)

    @abstractmethod
    def _ingest_tools(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def _answer_tools(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def _ingest_handlers(self) -> dict[str, Any]: ...

    @abstractmethod
    def _answer_handlers(self, retrieved_source_ids: list[str]) -> dict[str, Any]: ...
