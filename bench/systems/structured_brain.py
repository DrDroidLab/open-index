"""Structured memory arm using droid-brain typed doc_types."""

from __future__ import annotations

import shutil
import time
from typing import Any, Iterator

from droid_brain.brain import Brain
from droid_brain.models import Entity, Relationship

from bench.ir.types import EvidenceEvent, Question
from bench.llm.client import LLMClient
from bench.prompts import answer_system_prompt, ingest_system_prompt
from bench.systems._tool_loop import function_tool, object_schema, run_tool_loop
from bench.systems._utils import make_temp_brain, normalize_slug
from bench.systems.base import Answer, MemorySystem


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_INGEST_TOOLS = [
    function_tool(
        "write_fact",
        "Write a user_fact entity. Overwrite the same id to update a fact.",
        object_schema(
            {
                "subject": {"type": "string"},
                "attribute": {"type": "string"},
                "value": {"type": "string"},
            }
        ),
    ),
    function_tool(
        "write_preference",
        "Write a preference entity. Overwrite the same id to update a preference.",
        object_schema(
            {
                "subject": {"type": "string"},
                "preference_type": {"type": "string"},
                "value": {"type": "string"},
            }
        ),
    ),
    function_tool(
        "write_person",
        "Write a person entity. Overwrite the same id to update a person.",
        object_schema(
            {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "notes": {"type": "string"},
            }
        ),
    ),
    function_tool(
        "write_event",
        "Write an event entity.",
        object_schema(
            {
                "description": {"type": "string"},
                "date": {"type": "string"},
            }
        ),
    ),
    function_tool(
        "link_entities",
        "Add a relationship between two existing entities.",
        object_schema(
            {
                "from_id": {"type": "string"},
                "to_id": {"type": "string"},
                "relationship_meaning": {"type": "string"},
            }
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
        "search_brain",
        "Search the brain for relevant entities.",
        object_schema(
            {
                "query": {"type": "string"},
                "doc_types": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
            required=["query"],
        ),
    ),
    function_tool(
        "get_entity",
        "Retrieve one entity by id, including its relationships.",
        object_schema({"id": {"type": "string"}}, required=["id"]),
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


class StructuredBrainMemory(MemorySystem):
    """Structured droid-brain memory with typed doc_types and stable fact ids."""

    def __init__(
        self,
        llm_client: LLMClient,
        keep_state: bool = False,
        max_ingest_tools: int = 8,
        max_answer_tools: int = 12,
    ):
        self.client = llm_client
        self.keep_state = keep_state
        self.max_ingest_tools = max_ingest_tools
        self.max_answer_tools = max_answer_tools
        self._brain_dir = make_temp_brain("structured")
        self.brain = Brain.open(self._brain_dir)
        self._current_event: EvidenceEvent | None = None

    def ingest(self, events: Iterator[EvidenceEvent]) -> None:
        for event in events:
            self._current_event = event
            messages = [
                {"role": "system", "content": ingest_system_prompt("structured")},
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
        system_prompt = answer_system_prompt(
            "structured", question.question_timestamp
        )
        nav = self.brain.navigation_guidelines()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"Brain navigation guide:\n{nav}"},
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
        # De-duplicate while preserving order.
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

    # -- tool handlers --------------------------------------------------------

    def _ingest_handlers(self) -> dict[str, Any]:
        event = self._current_event
        source_id = event.source_id if event else ""
        timestamp = event.timestamp if event else ""

        def _write_fact(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            subject = args.get("subject", "")
            attribute = args.get("attribute", "")
            value = args.get("value", "")
            entity_id = f"user_fact:{normalize_slug(subject)}-{normalize_slug(attribute)}"
            self.brain.put_entity(
                Entity(
                    id=entity_id,
                    doc_type="user_fact",
                    name=f"{subject} {attribute}",
                    fields={
                        "subject": subject,
                        "attribute": attribute,
                        "value": value,
                        "source_id": source_id,
                        "timestamp": timestamp,
                    },
                )
            )
            return f"Wrote {entity_id}.", {}

        def _write_preference(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            subject = args.get("subject", "")
            preference_type = args.get("preference_type", "")
            value = args.get("value", "")
            entity_id = f"preference:{normalize_slug(subject)}-{normalize_slug(preference_type)}"
            self.brain.put_entity(
                Entity(
                    id=entity_id,
                    doc_type="preference",
                    name=f"{subject} {preference_type}",
                    fields={
                        "subject": subject,
                        "preference_type": preference_type,
                        "value": value,
                        "source_id": source_id,
                        "timestamp": timestamp,
                    },
                )
            )
            return f"Wrote {entity_id}.", {}

        def _write_person(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            name = args.get("name", "")
            role = args.get("role", "")
            notes = args.get("notes", "")
            entity_id = f"person:{normalize_slug(name)}"
            self.brain.put_entity(
                Entity(
                    id=entity_id,
                    doc_type="person",
                    name=name,
                    fields={
                        "name": name,
                        "role": role,
                        "notes": notes,
                        "source_id": source_id,
                        "timestamp": timestamp,
                    },
                )
            )
            return f"Wrote {entity_id}.", {}

        def _write_event(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            description = args.get("description", "")
            date = args.get("date", "")
            slug = normalize_slug(description[:60])
            entity_id = f"event:{normalize_slug(source_id)}-{slug}"
            self.brain.put_entity(
                Entity(
                    id=entity_id,
                    doc_type="event",
                    name=description[:80],
                    fields={
                        "description": description,
                        "date": date,
                        "source_id": source_id,
                        "timestamp": timestamp,
                    },
                )
            )
            return f"Wrote {entity_id}.", {}

        def _link_entities(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            from_id = args.get("from_id", "")
            to_id = args.get("to_id", "")
            meaning = args.get("relationship_meaning", "")
            entity = self.brain.get_entity(from_id)
            if entity is None:
                return f"Entity {from_id} not found.", {}
            if not any(r.target == to_id and r.relationship_edge_meaning == meaning for r in entity.related_to):
                entity.related_to.append(Relationship(target=to_id, relationship_edge_meaning=meaning))
            self.brain.put_entity(entity)
            return f"Linked {from_id} -> {to_id}.", {}

        def _done(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            return "Done.", {}

        return {
            "write_fact": _write_fact,
            "write_preference": _write_preference,
            "write_person": _write_person,
            "write_event": _write_event,
            "link_entities": _link_entities,
            "done": _done,
        }

    def _answer_handlers(self, retrieved_source_ids: list[str]) -> dict[str, Any]:
        def _search_brain(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            query = args.get("query", "")
            doc_types = args.get("doc_types")
            limit = args.get("limit", 5)
            results = self.brain.search(query=query, doc_types=doc_types, limit=limit)
            lines = [f"Search results ({results.total} total, limit {limit}):"]
            for r in results.results:
                raw = r.get("entity")
                if raw is None:
                    continue
                entity = Entity.from_dict(raw)
                sid = entity.fields.get("source_id")
                if sid and sid not in retrieved_source_ids:
                    retrieved_source_ids.append(sid)
                lines.append(
                    f"- id={entity.id} doc_type={entity.doc_type} name={entity.name} "
                    f"source_id={sid} fields={entity.fields}"
                )
            return "\n".join(lines), {}

        def _get_entity(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            entity_id = args.get("id", "")
            entity = self.brain.get_entity(entity_id)
            if entity is None:
                return f"Entity {entity_id} not found.", {}
            sid = entity.fields.get("source_id")
            if sid and sid not in retrieved_source_ids:
                retrieved_source_ids.append(sid)
            related = [f"{r.target} ({r.relationship_edge_meaning})" for r in entity.related_to]
            return (
                f"Entity {entity.id} ({entity.doc_type}): name={entity.name}, "
                f"source_id={sid}, fields={entity.fields}, related_to={related}",
                {},
            )

        def _answer(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            text = args.get("text", "")
            source_ids = list(args.get("source_ids", []))
            return "Answer recorded.", {"text": text, "source_ids": source_ids}

        return {
            "search_brain": _search_brain,
            "get_entity": _get_entity,
            "answer": _answer,
        }
