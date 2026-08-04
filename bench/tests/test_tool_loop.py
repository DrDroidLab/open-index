"""Tests for the generic tool-calling loop."""

from __future__ import annotations

from bench.llm.client import FakeLLMClient, Usage
from bench.systems._tool_loop import run_tool_loop


def _answer_handler(args: dict) -> tuple[str, dict]:
    return "Answer recorded.", {"text": args.get("text", ""), "source_ids": list(args.get("source_ids", []))}


def test_answer_tool_enforcement_accepts_tool() -> None:
    fake = FakeLLMClient()
    fake.queue_tool_calls([("answer", {"text": "final", "source_ids": ["s1"]})])
    result = run_tool_loop(
        client=fake,
        messages=[{"role": "user", "content": "q"}],
        tools=[],
        handlers={"answer": _answer_handler},
        finish_tool="answer",
        max_tool_calls=5,
        require_finish_tool=True,
    )
    assert result.content == "final"
    assert result.source_ids == ["s1"]
    assert result.metadata.get("fallback") is None


def test_answer_tool_enforcement_corrects_plain_text_then_tool() -> None:
    fake = FakeLLMClient()
    # First a plain-text response, then the answer tool.
    fake.queue_text("I think the answer is final.")
    fake.queue_tool_calls([("answer", {"text": "final", "source_ids": ["s1"]})])
    result = run_tool_loop(
        client=fake,
        messages=[{"role": "user", "content": "q"}],
        tools=[],
        handlers={"answer": _answer_handler},
        finish_tool="answer",
        max_tool_calls=5,
        require_finish_tool=True,
    )
    assert result.content == "final"
    assert result.source_ids == ["s1"]


def test_answer_tool_enforcement_fallback_on_exhaustion() -> None:
    fake = FakeLLMClient()
    # Two plain-text responses with a budget of 2 rounds -> fallback on the last.
    fake.queue_text("guess one")
    fake.queue_text("guess two")
    result = run_tool_loop(
        client=fake,
        messages=[{"role": "user", "content": "q"}],
        tools=[],
        handlers={"answer": _answer_handler},
        finish_tool="answer",
        max_tool_calls=2,
        require_finish_tool=True,
    )
    assert result.content == "guess two"
    assert result.metadata.get("fallback") is True


def test_ingest_plain_text_terminates_immediately() -> None:
    fake = FakeLLMClient()
    fake.queue_text("done")
    result = run_tool_loop(
        client=fake,
        messages=[{"role": "user", "content": "event"}],
        tools=[],
        handlers={"done": lambda args: ("done", {})},
        finish_tool="done",
        max_tool_calls=5,
    )
    assert result.content == "done"
    assert result.tool_calls == 0
