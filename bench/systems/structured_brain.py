"""Structured memory arm using droid-brain typed doc_types."""

from __future__ import annotations

from typing import Any

from droid_brain.models import Entity, Relationship

from bench.llm.client import LLMClient
from bench.systems._brain import BrainBackedMemorySystem
from bench.systems._tool_loop import answer_tool_handler, function_tool, object_schema
from bench.systems._utils import fact_entity_id, normalize_slug


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


class StructuredBrainMemory(BrainBackedMemorySystem):
    """Structured droid-brain memory with typed doc_types and stable fact ids."""

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
            config_name="structured",
            prompt_variant="structured",
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
        def _write_fact(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            subject = args.get("subject", "")
            attribute = args.get("attribute", "")
            value = args.get("value", "")
            entity_id = fact_entity_id(subject, attribute)
            observation = self._put_entity(
                entity_id,
                "user_fact",
                f"{subject} {attribute}",
                {"subject": subject, "attribute": attribute, "value": value},
            )
            return observation, {}

        def _write_preference(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            subject = args.get("subject", "")
            preference_type = args.get("preference_type", "")
            value = args.get("value", "")
            entity_id = f"preference:{normalize_slug(subject)}-{normalize_slug(preference_type)}"
            observation = self._put_entity(
                entity_id,
                "preference",
                f"{subject} {preference_type}",
                {"subject": subject, "preference_type": preference_type, "value": value},
            )
            return observation, {}

        def _write_person(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            name = args.get("name", "")
            role = args.get("role", "")
            notes = args.get("notes", "")
            entity_id = f"person:{normalize_slug(name)}"
            observation = self._put_entity(
                entity_id,
                "person",
                name,
                {"name": name, "role": role, "notes": notes},
            )
            return observation, {}

        def _write_event(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            description = args.get("description", "")
            date = args.get("date", "")
            slug = normalize_slug(description[:60])
            entity_id = f"event:{normalize_slug(self._current_source_id)}-{slug}"
            observation = self._put_entity(
                entity_id,
                "event",
                description[:80],
                {"description": description, "date": date},
            )
            return observation, {}

        def _link_entities(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            from_id = args.get("from_id", "")
            to_id = args.get("to_id", "")
            meaning = args.get("relationship_meaning", "")
            entity = self.brain.get_entity(from_id)
            if entity is None:
                return f"Entity {from_id} not found.", {}
            if not any(
                r.target == to_id and r.relationship_edge_meaning == meaning for r in entity.related_to
            ):
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
            limit = min(args.get("limit", self._retrieval_k), self._retrieval_k)
            results = self.brain.search(query=query, doc_types=doc_types, limit=limit)
            lines = [f"Search results ({results.total} total, limit {limit}):"]
            lines.extend(
                self._format_search_result_lines(
                    results,
                    retrieved_source_ids,
                    lambda e: (
                        f"- id={e.id} doc_type={e.doc_type} name={e.name} "
                        f"source_id={e.fields.get('source_id')} fields={e.fields}"
                    ),
                )
            )
            return "\n".join(lines), {}

        def _get_entity(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            entity_id = args.get("id", "")
            entity = self.brain.get_entity(entity_id)
            if entity is None:
                return f"Entity {entity_id} not found.", {}
            record_source_id(entity, retrieved_source_ids)
            related = [f"{r.target} ({r.relationship_edge_meaning})" for r in entity.related_to]
            return (
                f"Entity {entity.id} ({entity.doc_type}): name={entity.name}, "
                f"source_id={entity.fields.get('source_id')}, fields={entity.fields}, "
                f"related_to={related}",
                {},
            )

        return {
            "search_brain": _search_brain,
            "get_entity": _get_entity,
            "answer": answer_tool_handler,
        }
