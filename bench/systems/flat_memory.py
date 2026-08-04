"""Flat memory baseline: one unstructured doc_type, same SQLite+FTS5 backend."""

from __future__ import annotations

from typing import Any

from bench.llm.client import LLMClient
from bench.systems._brain import BrainBackedMemorySystem
from bench.systems._tool_loop import answer_tool_handler, function_tool, object_schema
from bench.systems._utils import normalize_slug, record_source_id


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


class FlatMemoryBaseline(BrainBackedMemorySystem):
    """Flat baseline: a single `memory` doc_type, same FTS5 backend as structured."""

    def __init__(
        self,
        llm_client: LLMClient,
        keep_state: bool = False,
        max_ingest_tools: int = 8,
        max_answer_tools: int = 12,
        k: int = 5,
        seed: int = 42,
    ):
        super().__init__(
            llm_client,
            config_name="flat",
            prompt_variant="flat",
            keep_state=keep_state,
            max_ingest_tools=max_ingest_tools,
            max_answer_tools=max_answer_tools,
            k=k,
            seed=seed,
        )

    def _ingest_tools(self) -> list[dict[str, Any]]:
        return _INGEST_TOOLS

    def _answer_tools(self) -> list[dict[str, Any]]:
        return _ANSWER_TOOLS

    def _ingest_handlers(self) -> dict[str, Any]:
        def _write_memory(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            text = args.get("text", "")
            memory_id = args.get("memory_id")
            source_id = self._current_source_id
            entity_id = (
                f"memory:{normalize_slug(memory_id)}"
                if memory_id
                else f"memory:{normalize_slug(source_id)}"
            )
            observation = self._put_entity(
                entity_id,
                "memory",
                entity_id,
                {"text": text},
            )
            return observation, {}

        def _done(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            return "Done.", {}

        return {"write_memory": _write_memory, "done": _done}

    def _answer_handlers(self, retrieved_source_ids: list[str]) -> dict[str, Any]:
        def _search_memory(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            query = args.get("query", "")
            limit = min(args.get("limit", self._retrieval_k), self._retrieval_k)
            results = self.brain.search(query=query, doc_types=["memory"], limit=limit)
            lines = [f"Memory search results ({results.total} total, limit {limit}):"]
            lines.extend(
                self._format_search_result_lines(
                    results,
                    retrieved_source_ids,
                    lambda e: (
                        f"- id={e.id} source_id={e.fields.get('source_id')} "
                        f"text={e.fields.get('text', '')[:200]}"
                    ),
                )
            )
            return "\n".join(lines), {}

        def _get_memory(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            memory_id = args.get("memory_id", "")
            entity_id = memory_id if ":" in memory_id else f"memory:{normalize_slug(memory_id)}"
            entity = self.brain.get_entity(entity_id)
            if entity is None:
                return f"Memory {entity_id} not found.", {}
            record_source_id(entity, retrieved_source_ids)
            return (
                f"Memory {entity.id}: source_id={entity.fields.get('source_id')}, "
                f"text={entity.fields.get('text', '')}",
                {},
            )

        return {
            "search_memory": _search_memory,
            "get_memory": _get_memory,
            "answer": answer_tool_handler,
        }
