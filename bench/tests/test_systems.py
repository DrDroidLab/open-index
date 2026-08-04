"""Tests for the memory-system arms and shared utilities."""

from __future__ import annotations

import pytest

from bench.ir.types import EvidenceEvent, Question
from bench.llm.client import FakeLLMClient, ToolCall, Usage
from bench.systems import FlatMemoryBaseline, LongContextBaseline, StructuredBrainMemory
from bench.systems._utils import normalize_slug


def test_normalize_slug_basic() -> None:
    assert normalize_slug("current manager") == "current-manager"
    assert normalize_slug("  User  Name  ") == "user-name"
    assert normalize_slug("a!b@c#d") == "abcd"


def test_normalize_slug_empty_fallback() -> None:
    assert normalize_slug("!!!") == "x"


def test_structured_ingest_and_answer_with_fake_llm() -> None:
    fake = FakeLLMClient()
    # Ingest: one tool call writes a fact, then the model calls done.
    fake.queue_tool_calls(
        [
            (
                "write_fact",
                {"subject": "user", "attribute": "manager", "value": "Alice"},
            )
        ],
        usage=Usage(40, 10),
    )
    fake.queue_tool_calls([("done", {})], usage=Usage(10, 5))

    # Answer: search then answer.
    fake.queue_tool_calls(
        [("search_brain", {"query": "manager", "doc_types": ["user_fact"], "limit": 5})],
        usage=Usage(30, 10),
    )
    fake.queue_tool_calls(
        [("answer", {"text": "Alice is the manager.", "source_ids": ["s1"]})],
        usage=Usage(20, 10),
    )

    system = StructuredBrainMemory(fake)
    try:
        event = EvidenceEvent(
            event_id="e1", source_id="s1", timestamp="2023-05-20", text="Manager is Alice."
        )
        system.ingest(iter([event]))
        answer = system.answer(Question(question_id="q1", text="Who is the manager?"))
    finally:
        system.close()

    assert answer.text == "Alice is the manager."
    assert "s1" in answer.source_ids


def test_structured_slug_normalization_in_tool_handler() -> None:
    fake = FakeLLMClient()
    fake.queue_tool_calls(
        [
            (
                "write_fact",
                {"subject": "Current Manager", "attribute": "Name", "value": "Bob"},
            )
        ]
    )
    fake.queue_tool_calls([("done", {})])

    system = StructuredBrainMemory(fake)
    try:
        system.ingest(
            iter(
                [
                    EvidenceEvent(
                        event_id="e1", source_id="s1", timestamp="2023-05-20", text="x"
                    )
                ]
            )
        )
        entity = system.brain.get_entity("user_fact:current-manager-name")
        assert entity is not None
        assert entity.fields["value"] == "Bob"
    finally:
        system.close()


def test_flat_ingest_and_answer_with_fake_llm() -> None:
    fake = FakeLLMClient()
    fake.queue_tool_calls([("write_memory", {"text": "Manager is Alice."})])
    fake.queue_tool_calls([("done", {})])

    fake.queue_tool_calls([("search_memory", {"query": "manager", "limit": 5})])
    fake.queue_tool_calls(
        [("answer", {"text": "Alice", "source_ids": ["s1"]})]
    )

    system = FlatMemoryBaseline(fake)
    try:
        system.ingest(
            iter(
                [
                    EvidenceEvent(
                        event_id="e1", source_id="s1", timestamp="2023-05-20", text="x"
                    )
                ]
            )
        )
        answer = system.answer(Question(question_id="q1", text="Who is the manager?"))
    finally:
        system.close()

    assert answer.text == "Alice"
    assert "s1" in answer.source_ids


def test_long_context_truncation_accounting() -> None:
    fake = FakeLLMClient()
    fake.queue_text("Answer: latest")

    system = LongContextBaseline(fake, context_budget=100)
    # Create many short events that exceed the 100-token budget.
    events = [
        EvidenceEvent(
            event_id=f"e{i}", source_id=f"s{i}", timestamp=str(i), text=f"Event number {i} has some words."
        )
        for i in range(30)
    ]
    system.ingest(iter(events))
    answer = system.answer(Question(question_id="q1", text="What is the latest?"))

    assert answer.text == "Answer: latest"
    assert answer.metadata["truncated"] is True
    assert answer.metadata["dropped_events"] > 0
    assert answer.metadata["context_tokens"] <= 100


def test_long_context_respects_question_date() -> None:
    fake = FakeLLMClient()
    fake.queue_text("latest")

    system = LongContextBaseline(fake, context_budget=1000)
    system.ingest(
        iter(
            [
                EvidenceEvent(
                    event_id="e1", source_id="s1", timestamp="2023-05-20", text="old"
                ),
                EvidenceEvent(
                    event_id="e2", source_id="s2", timestamp="2023-05-22", text="new"
                ),
            ]
        )
    )
    answer = system.answer(
        Question(question_id="q1", text="x", question_timestamp="2023-05-21")
    )
    assert answer.text == "latest"


def test_ingest_tool_budget_parity() -> None:
    from bench.llm.client import LLMClient

    # Flat default must equal structured default (8 per plan review).
    assert FlatMemoryBaseline.__init__.__defaults__[1] == 8
    assert StructuredBrainMemory.__init__.__defaults__[1] == 8


def test_structured_retrieval_budget_caps_source_ids() -> None:
    """Search limit and recorded source_ids are capped to k."""
    fake = FakeLLMClient()
    # Ingest 3 events as 3 facts (same subject, different attributes so different ids).
    for i in range(3):
        fake.queue_tool_calls(
            [("write_fact", {"subject": "user", "attribute": f"attr{i}", "value": f"v{i}"})]
        )
        fake.queue_tool_calls([("done", {})])

    # Answer phase: search returns all 3; answer tool supplies no source_ids.
    fake.queue_tool_calls([("search_brain", {"query": "user", "limit": 10})])
    fake.queue_tool_calls([("answer", {"text": "summary", "source_ids": []})])

    system = StructuredBrainMemory(fake, k=2)
    try:
        events = [
            EvidenceEvent(
                event_id=f"e{i}", source_id=f"s{i}", timestamp="2023-05-20", text="x"
            )
            for i in range(3)
        ]
        system.ingest(iter(events))
        answer = system.answer(Question(question_id="q1", text="summary?"))
    finally:
        system.close()

    # The search tool was clamped to limit 2, so only 2 source_ids are recorded.
    assert len(answer.source_ids) <= 2


def test_seed_is_forwarded_from_systems() -> None:
    fake = FakeLLMClient()
    fake.queue_tool_calls([("write_fact", {"subject": "user", "attribute": "manager", "value": "A"})])
    fake.queue_tool_calls([("done", {})])
    fake.queue_tool_calls([("answer", {"text": "A", "source_ids": ["s1"]})])

    system = StructuredBrainMemory(fake, seed=123)
    try:
        system.ingest(
            iter(
                [
                    EvidenceEvent(
                        event_id="e1", source_id="s1", timestamp="2023-05-20", text="x"
                    )
                ]
            )
        )
        system.answer(Question(question_id="q1", text="x"))
    finally:
        system.close()

    # All chat calls (ingest + answer) should have used seed=123.
    assert all(s == 123 for s in fake.seeds)


def test_ingest_filters_events_after_question_timestamp() -> None:
    fake = FakeLLMClient()
    # Only the first event should be processed.
    fake.queue_tool_calls([("write_fact", {"subject": "user", "attribute": "manager", "value": "A"})])
    fake.queue_tool_calls([("done", {})])
    fake.queue_tool_calls([("answer", {"text": "A", "source_ids": ["s0"]})])

    system = StructuredBrainMemory(fake)
    try:
        system.ingest(
            iter(
                [
                    EvidenceEvent(
                        event_id="e1", source_id="s0", timestamp="2023-05-20", text="x"
                    ),
                    EvidenceEvent(
                        event_id="e2", source_id="s1", timestamp="2023-05-22", text="y"
                    ),
                ]
            ),
            question_timestamp="2023-05-21",
        )
        answer = system.answer(Question(question_id="q1", text="x"))
    finally:
        system.close()

    assert answer.metadata.get("ingest_dropped_after_date") == 1
    # Second event should not have created an entity.
    assert system.brain.get_entity("user_fact:manager") is None
    assert system.brain.get_entity("user_fact:user-manager") is not None


def test_long_context_counts_full_prompt_and_reports_source_ids() -> None:
    fake = FakeLLMClient()
    fake.queue_text("Answer")

    # Small budget so only a few events fit; system prompt must be counted.
    system = LongContextBaseline(fake, context_budget=50)
    events = [
        EvidenceEvent(
            event_id=f"e{i}", source_id=f"s{i}", timestamp=str(i), text=f"Event content {i}."
        )
        for i in range(10)
    ]
    system.ingest(iter(events))
    answer = system.answer(Question(question_id="q1", text="What?"))

    assert answer.text == "Answer"
    assert answer.metadata["truncated"] is True
    assert answer.metadata["dropped_events"] > 0
    # The included events list is the retrieval set; source_ids must match it.
    included_count = answer.metadata["included_events"]
    assert len(answer.source_ids) == included_count
    assert included_count < 10


def test_flat_retrieval_budget_caps_source_ids() -> None:
    fake = FakeLLMClient()
    for i in range(3):
        fake.queue_tool_calls([("write_memory", {"text": f"memory {i}"})])
        fake.queue_tool_calls([("done", {})])
    fake.queue_tool_calls([("search_memory", {"query": "memory", "limit": 10})])
    fake.queue_tool_calls([("answer", {"text": "summary", "source_ids": []})])

    system = FlatMemoryBaseline(fake, k=2)
    try:
        events = [
            EvidenceEvent(
                event_id=f"e{i}", source_id=f"s{i}", timestamp="2023-05-20", text="x"
            )
            for i in range(3)
        ]
        system.ingest(iter(events))
        answer = system.answer(Question(question_id="q1", text="summary?"))
    finally:
        system.close()

    assert len(answer.source_ids) <= 2


def test_ingest_prompt_includes_question_timestamp() -> None:
    fake = FakeLLMClient()
    fake.queue_tool_calls([("done", {})])
    fake.queue_tool_calls([("answer", {"text": "x", "source_ids": ["s0"]})])

    system = FlatMemoryBaseline(fake)
    try:
        system.ingest(
            iter(
                [
                    EvidenceEvent(
                        event_id="e1", source_id="s0", timestamp="2023-05-20", text="x"
                    )
                ]
            ),
            question_timestamp="2023-05-21",
        )
        system.answer(Question(question_id="q1", text="x"))
    finally:
        system.close()

    # The first chat call is the ingest loop; its system prompt should mention the date.
    first_messages = fake.calls[0]
    system_prompt = first_messages[0]["content"]
    assert "2023-05-21" in system_prompt
    assert "dated on or before" in system_prompt
