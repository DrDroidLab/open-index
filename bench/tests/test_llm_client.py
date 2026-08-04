"""Tests for the LLM client wrapper and fake client."""

from __future__ import annotations

import pytest

from bench.llm.client import ChatResponse, CostLedger, FakeLLMClient, LLMClient, ToolCall, Usage


def test_cost_ledger_adds_usage_and_cost_for_mini() -> None:
    ledger = CostLedger()
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=500_000)
    ledger.add(usage, "gpt-4o-mini")
    assert ledger.prompt_tokens == 1_000_000
    assert ledger.completion_tokens == 500_000
    # $0.15/1M in + $0.60/1M out
    assert abs(ledger.total_cost_usd - (0.15 + 0.30)) < 1e-9


def test_cost_ledger_adds_usage_and_cost_for_4o() -> None:
    ledger = CostLedger()
    usage = Usage(prompt_tokens=2_000_000, completion_tokens=0)
    ledger.add(usage, "gpt-4o")
    assert ledger.total_cost_usd == 5.0


def test_fake_llm_client_queues_and_records_calls() -> None:
    fake = FakeLLMClient()
    fake.queue_text("hello")
    fake.queue_tool_calls(
        [("write_fact", {"subject": "user", "attribute": "manager", "value": "Alice"})],
        usage=Usage(20, 10),
    )

    r1 = fake.chat([{"role": "user", "content": "hi"}])
    assert r1.content == "hello"

    r2 = fake.chat([{"role": "user", "content": "hi"}], tools=[])
    assert r2.tool_calls[0].name == "write_fact"
    assert r2.tool_calls[0].arguments == {"subject": "user", "attribute": "manager", "value": "Alice"}
    assert r2.usage.prompt_tokens == 20

    assert len(fake.calls) == 2
    assert fake.ledger.prompt_tokens == 30


def test_fake_llm_client_raises_when_empty() -> None:
    fake = FakeLLMClient()
    with pytest.raises(RuntimeError, match="empty"):
        fake.chat([{"role": "user", "content": "x"}])
