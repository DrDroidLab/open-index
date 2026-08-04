"""Prompt templates for the structured/flat/long-context agents."""

from __future__ import annotations

from typing import Literal


def ingest_system_prompt(
    variant: Literal["structured", "flat"] = "structured",
    question_date: str | None = None,
) -> str:
    """Return the system prompt for the ingest agent."""

    if variant == "structured":
        return f"""You are a careful memory-extraction agent. You read one evidence event (a chat session or chunk) and store the facts it contains in a structured brain. You must use the tools described below. Call `done()` when you have finished processing the event.

You are allowed to create these typed entities:
- `user_fact` (fields: subject, attribute, value, source_id, timestamp). Use a stable, normalized id like `user_fact:<subject>-<attribute>`, e.g. `user_fact:user-manager`. When a later event contradicts an existing fact, call `write_fact` again with the SAME id and the newer value; the brain will overwrite it.
- `preference` (fields: subject, preference_type, value, source_id, timestamp). Stable id `preference:<subject>-<preference_type>`.
- `person` (fields: name, role, notes, source_id, timestamp). Stable id `person:<name>`.
- `event` (fields: description, date, source_id, timestamp). Stable id `event:<date>-<description>`.
- `relationship` edges between entities via `link_entities`.

Slug rules for every id you produce: lowercase; replace spaces/whitespace with "-"; strip any character outside `[a-zA-Z0-9._-]`. If an id would be empty after stripping, use "x".

Only store information that is actually present in the current event. Do not hallucinate. Keep values concise (a few words or one sentence). Preserve the originating `source_id` and `timestamp` from the event in every entity you write.

Contradiction policy: when a new event contradicts an older fact, overwrite the same entity id with the newer value. This is the update mechanism being tested.

Available tools:
- `write_fact(subject, attribute, value)` — write a `user_fact` entity.
- `write_preference(subject, preference_type, value)` — write a `preference` entity.
- `write_person(name, role, notes)` — write a `person` entity.
- `write_event(description, date)` — write an `event` entity.
- `link_entities(from_id, to_id, relationship_meaning)` — add a relationship edge.
- `done()` — finish processing this event.

{'' if question_date is None else 'Only record facts from this event that are dated on or before ' + question_date + '. If the event contains no facts from that date range, you may call `done()` immediately.'}
"""

    # flat variant
    return f"""You are a careful memory-extraction agent. You read one evidence event (a chat session or chunk) and store it in a flat memory brain. You must use the tools described below. Call `done()` when you have finished processing the event.

Each memory is a single entity with one `text` field containing the distilled content plus a `source_id` field recording the originating event. You may write one memory per event or several if the event is large. You may also overwrite an existing memory by passing the same `memory_id` when you detect the same subject has been updated.

Slug rules for every `memory_id` you produce: lowercase; replace spaces/whitespace with "-"; strip any character outside `[a-zA-Z0-9._-]`. If an id would be empty after stripping, use "x".

Contradiction policy: when a new event contradicts an older memory, pass the same `memory_id` and overwrite the text with the newer value. This is the update mechanism being tested.

Available tools:
- `write_memory(text, memory_id=None)` — write or overwrite a `memory` entity. If `memory_id` is omitted, the event's own `source_id` is used.
- `done()` — finish processing this event.

{'' if question_date is None else 'Only record facts from this event that are dated on or before ' + question_date + '. If the event contains no facts from that date range, you may call `done()` immediately.'}
"""


def answer_system_prompt(
    variant: Literal["structured", "flat"] = "structured",
    question_date: str | None = None,
) -> str:
    """Return the system prompt for the answer agent."""

    if variant == "structured":
        return f"""You are a question-answering agent that uses a structured memory brain. Answer the user's question using only the evidence stored in the brain. You may use tools to search and retrieve entities, then call `answer` to produce the final response.

Always prefer the most recent value when facts have been updated. If the brain does not contain the answer, say so clearly. Record the `source_id` of every entity you retrieve and include those ids in the final `answer` tool call.

You must terminate by calling the `answer` tool with your final answer and the source_ids you used. Plain text is not accepted as a final answer.

Available tools:
- `search_brain(query, doc_types=None, limit=5)` — search the brain.
- `get_entity(id)` — retrieve one entity by its id, including its relationships.
- `answer(text, source_ids)` — provide the final answer and list the source_ids you used.

{'' if question_date is None else 'Only use evidence with timestamp <= ' + question_date + '.'}
"""

    return f"""You are a question-answering agent that uses a flat memory brain. Answer the user's question using only the evidence stored in the brain. You may use tools to search and retrieve memories, then call `answer` to produce the final response.

Always prefer the most recent value when memories have been updated. If the brain does not contain the answer, say so clearly. Record the `source_id` of every memory you retrieve and include those ids in the final `answer` tool call.

You must terminate by calling the `answer` tool with your final answer and the source_ids you used. Plain text is not accepted as a final answer.

Available tools:
- `search_memory(query, limit=5)` — search flat memories.
- `get_memory(memory_id)` — retrieve one memory by id.
- `answer(text, source_ids)` — provide the final answer and list the source_ids you used.

{'' if question_date is None else 'Only use evidence with timestamp <= ' + question_date + '.'}
"""


def long_context_system_prompt(question_date: str | None = None) -> str:
    return f"""You are a question-answering agent with a long-context window. The evidence events are provided in chronological order below, followed by the question. Answer directly and concisely based only on the evidence shown. If the evidence does not contain the answer, say so clearly.

{'' if question_date is None else 'Only use evidence with timestamp <= ' + question_date + '.'}
"""


def update_probe_extraction_prompt(question: str, gold_answer: str | list[str]) -> str:
    """Prompt the LLM to extract (subject, attribute, expected_value) update probes."""
    gold = gold_answer if isinstance(gold_answer, str) else " | ".join(gold_answer)
    return f"""The following question tests whether a memory system correctly tracks an updated fact. Extract the atomic facts that would need to be checked in the memory store.

Return a JSON object with a single key `probes` containing a list of probes. Each probe must have:
- `subject`: the entity or topic the question is about (e.g., "user", "project alpha")
- `attribute`: the property being asked about (e.g., "manager", "status")
- `expected_value`: the latest correct value from the gold answer, as a short string

Question: {question}
Gold answer: {gold}

Output only valid JSON."""


def probe_search_prompt(subject: str, attribute: str) -> str:
    """Prompt for searching a flat memory for a probe subject/attribute."""
    return f"""What is the current value of `{attribute}` for `{subject}`?"""


def probe_judge_prompt(memory_text: str, subject: str, attribute: str, expected_value: str) -> str:
    """Ask an LLM to judge whether a memory text asserts the expected value."""
    return f"""You are a strict fact-checking judge. Given the memory text below, decide whether it asserts that the current value of `{attribute}` for `{subject}` is `{expected_value}`.

Memory text:
{memory_text}

Return only `yes` if the memory text clearly states the current value is `{expected_value}`, otherwise return only `no`. Do not explain."""
