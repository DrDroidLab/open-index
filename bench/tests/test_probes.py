"""Tests for update probe extraction and validation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bench.harness.probes import (
    UpdateProbe,
    _is_update_question,
    _value_fuzzy_match,
    extract_probes,
    load_probes,
    probe_flat,
    probe_long_context,
    probe_structured,
)
from bench.ir.types import BenchmarkInstance, EvidenceEvent, Question
from bench.llm.client import FakeLLMClient
from bench.systems import FlatMemoryBaseline, StructuredBrainMemory


def test_value_fuzzy_match() -> None:
    assert _value_fuzzy_match("Alice", ["Alice Smith"]) is True
    assert _value_fuzzy_match("alice", ["Alice Smith"]) is True
    assert _value_fuzzy_match("Bob", ["Alice Smith"]) is False
    assert _value_fuzzy_match("", ["Alice"]) is False


def test_is_update_question() -> None:
    q = Question(question_id="q", text="x", ability="knowledge-update")
    assert _is_update_question(q, {}) is True
    q2 = Question(question_id="q", text="x", ability="single-session-user")
    assert _is_update_question(q2, {}) is False
    q3 = Question(question_id="q", text="x", ability="x")
    assert _is_update_question(q3, {"coarse_group": "conflict_resolution_sh"}) is True


def test_extract_probes_validates_and_caches() -> None:
    fake = FakeLLMClient()
    # Response contains a matching probe and a non-matching probe.
    fake.queue_text(
        '{"probes": [{"subject": "user", "attribute": "manager", "expected_value": "Alice"}, '
        '{"subject": "user", "attribute": "manager", "expected_value": "Bob"}]}'
    )
    instance = BenchmarkInstance(
        instance_id="i1",
        events=[],
        questions=[
            Question(
                question_id="q1",
                text="Who is the manager?",
                gold_answers=["Alice Smith"],
                ability="knowledge-update",
            )
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "probes.json"
        probes = extract_probes([instance], fake, cache_path=cache_path)

        assert len(probes) == 1
        assert probes[0].expected_value == "Alice"
        assert cache_path.exists()
        loaded = load_probes(cache_path)
        assert loaded == probes


def test_probe_structured_returns_latest_value() -> None:
    fake = FakeLLMClient()
    # Ingest two contradicting facts; same entity id should be overwritten.
    fake.queue_tool_calls([("write_fact", {"subject": "user", "attribute": "manager", "value": "Bob"})])
    fake.queue_tool_calls([("done", {})])
    fake.queue_tool_calls([("write_fact", {"subject": "user", "attribute": "manager", "value": "Alice"})])
    fake.queue_tool_calls([("done", {})])

    system = StructuredBrainMemory(fake)
    try:
        system.ingest(
            iter(
                [
                    EvidenceEvent(
                        event_id="e1", source_id="s1", timestamp="2023-05-20", text="x"
                    ),
                    EvidenceEvent(
                        event_id="e2", source_id="s2", timestamp="2023-05-22", text="x"
                    ),
                ]
            )
        )
        probe = UpdateProbe("q1", "user", "manager", "Alice")
        assert probe_structured(system.brain, probe) is True
        probe2 = UpdateProbe("q1", "user", "manager", "Bob")
        assert probe_structured(system.brain, probe2) is False
    finally:
        system.close()


def test_probe_flat_with_judge() -> None:
    fake = FakeLLMClient()
    # Ingest then search: the search tool call is never executed by the fake
    # (we exercise the flat probing path directly by writing to the brain first).
    fake.queue_tool_calls([("write_memory", {"text": "The manager is Alice now."})])
    fake.queue_tool_calls([("done", {})])

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
        judge_fake = FakeLLMClient()
        judge_fake.queue_text("yes")
        probe = UpdateProbe("q1", "user", "manager", "Alice")
        assert probe_flat(system.brain, probe, judge_fake) is True
    finally:
        system.close()


def test_probe_long_context() -> None:
    fake = FakeLLMClient()
    fake.queue_text("Alice")
    events = [
        EvidenceEvent(event_id="e1", source_id="s1", timestamp="2023-05-20", text="Manager is Bob."),
        EvidenceEvent(event_id="e2", source_id="s2", timestamp="2023-05-22", text="Manager is Alice."),
    ]
    probe = UpdateProbe("q1", "user", "manager", "Alice")
    assert probe_long_context(events, probe, fake) is True


def test_fact_entity_id_normalizes_multi_word_subject() -> None:
    """Multi-word subjects are slugified so the probe and entity IDs match."""
    from bench.systems._utils import fact_entity_id

    assert fact_entity_id("Project Alpha", "status") == "user_fact:project-alpha-status"
    assert fact_entity_id("project alpha", "status") == "user_fact:project-alpha-status"
    assert fact_entity_id(" current  manager ", "name") == "user_fact:current-manager-name"
