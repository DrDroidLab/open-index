"""Flat memory baseline: one unstructured doc_type, same SQLite+FTS5 backend."""

from __future__ import annotations

import shutil
import time
from typing import Any, Iterator

from droid_brain.brain import Brain
from droid_brain.models import Entity

from bench.ir.types import EvidenceEvent, Question
from bench.llm.client import LLMClient, Usage
from bench.prompts import answer_system_prompt, ingest_system_prompt
from bench.systems._tool_loop import function_tool, object_schema, run_tool_loop
from bench.systems._utils import make_temp_brain, normalize_slug
from bench.systems.base import Answer, MemorySystem


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_INGEST_TOOLS = [
    function_tool(
        "write_memory",
        "Write or overwrite a memory entity. Omit memory_id to use the event's source_id.",
        object_schema(
            {
                "text": {"type": "string"},
                "memory_id": {"type": "string"},
            },
            required=["text"],
        ),
    ),
    function_tool(
        "done",
        "Finish processing the current event.",
        object_schema({}, required=[]),
    ),
]

_ANSWER_TOOLS = [
    function_tool(
        "search_memory",
        "Search flat memories.",
        object_schema(
            {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            required=["query"],
        ),
    ),
    function_tool(
        "get_memory",
        "Retrieve one memory by id.",
        object_schema({"memory_id": {"type": "string"}}, required=["memory_id"]),
    ),
    function_tool(
        "answer",
        "Provide the final answer and list the source_ids used.",
        object_schema(
            {
                "text": {"type": "string"},
                "source_ids": {"type": "array", "items": {"type": "string"}},
            },
            required=["text"],
        ),
    ),
]


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


class FlatMemoryBaseline(MemorySystem):
    """Flat baseline: a single `memory` doc_type, same FTS5 backend as structured."""

    def __init__(
        self,
        llm_client: LLMClient,
        keep_state: bool = False,
        max_ingest_tools: int = 4,
        max_answer_tools: int = 12,
    ):
        self.client = llm_client
        self.keep_state = keep_state
        self.max_ingest_tools = max_ingest_tools
        self.max_answer_tools = max_answer_tools
        self._brain_dir = make_temp_brain("flat")
        self.brain = Brain.open(self._brain_dir)
        self._current_event: EvidenceEvent | None = None

    def ingest(self, events: Iterator[EvidenceEvent]) -> None:
        for event in events:
            self._current_event = event
            messages = [
                {"role": "system", "content": ingest_system_prompt("flat")},
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
                tools=_INGEST_TOOLS,
                handlers=self._ingest_handlers(),
                finish_tool="done",
                max_tool_calls=self.max_ingest_tools,
            )

    def answer(self, question: Question) -> Answer:
        start = time.perf_counter()
        retrieved_source_ids: list[str] = []
        system_prompt = answer_system_prompt("flat", question.question_timestamp)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question.text},
        ]
        result = run_tool_loop(
            client=self.client,
            messages=messages,
            tools=_ANSWER_TOOLS,
            handlers=self._answer_handlers(retrieved_source_ids),
            finish_tool="answer",
            max_tool_calls=self.max_answer_tools,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        answer_usage = result.usage
        all_source_ids = list(dict.fromkeys(result.source_ids + retrieved_source_ids))
        return Answer(
            text=result.content,
            source_ids=all_source_ids,
            tool_calls=result.tool_calls,
            usage=answer_usage,
            latency_ms=latency_ms,
            metadata=result.metadata,
        )

    def close(self) -> None:
        if not self.keep_state and self._brain_dir.exists():
            shutil.rmtree(self._brain_dir, ignore_errors=True)

    def _ingest_handlers(self) -> dict[str, Any]:
        event = self._current_event
        source_id = event.source_id if event else ""
        timestamp = event.timestamp if event else ""

        def _write_memory(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            text = args.get("text", "")
            memory_id = args.get("memory_id")
            if memory_id:
                entity_id = f"memory:{normalize_slug(memory_id)}"
            else:
                entity_id = f"memory:{normalize_slug(source_id)}"
            self.brain.put_entity(
                Entity(
                    id=entity_id,
                    doc_type="memory",
                    name=entity_id,
                    fields={
                        "text": text,
                        "source_id": source_id,
                        "timestamp": timestamp,
                    },
                )
            )
            return f"Wrote {entity_id}.", {}

        def _done(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            return "Done.", {}

        return {"write_memory": _write_memory, "done": _done}

    def _answer_handlers(self, retrieved_source_ids: list[str]) -> dict[str, Any]:
        def _search_memory(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            query = args.get("query", "")
            limit = args.get("limit", 5)
            results = self.brain.search(query=query, doc_types=["memory"], limit=limit)
            lines = [f"Memory search results ({results.total} total, limit {limit}):"]
            for r in results.results:
                raw = r.get("entity")
                if raw is None:
                    continue
                entity = Entity.from_dict(raw)
                sid = entity.fields.get("source_id")
                if sid and sid not in retrieved_source_ids:
                    retrieved_source_ids.append(sid)
                lines.append(
                    f"- id={entity.id} source_id={sid} text={entity.fields.get('text', '')[:200]}"
                )
            return "\n".join(lines), {}

        def _get_memory(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            memory_id = args.get("memory_id", "")
            entity_id = memory_id if ":" in memory_id else f"memory:{normalize_slug(memory_id)}"
            entity = self.brain.get_entity(entity_id)
            if entity is None:
                return f"Memory {entity_id} not found.", {}
            sid = entity.fields.get("source_id")
            if sid and sid not in retrieved_source_ids:
                retrieved_source_ids.append(sid)
            return (
                f"Memory {entity.id}: source_id={sid}, text={entity.fields.get('text', '')}",
                {},
            )

        def _answer(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            text = args.get("text", "")
            source_ids = list(args.get("source_ids", []))
            return "Answer recorded.", {"text": text, "source_ids": source_ids}

        return {
            "search_memory": _search_memory,
            "get_memory": _get_memory,
            "answer": _answer,
        }
