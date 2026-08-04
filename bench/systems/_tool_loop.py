"""Generic tool-calling loop used by the structured and flat arms."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from bench.llm.client import ChatResponse, LLMClient, Usage, build_tool_message


@dataclass
class ToolLoopResult:
    content: str = ""
    tool_calls: int = 0
    source_ids: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Build an OpenAI `function` tool definition."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties.keys()),
    }


def run_tool_loop(
    client: LLMClient,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    handlers: dict[str, Callable[[dict[str, Any]], tuple[str, Any]]],
    finish_tool: str = "answer",
    max_tool_calls: int = 15,
) -> ToolLoopResult:
    """Run a tool-calling loop until `finish_tool` is called or the budget is exhausted.

    Each handler receives the tool arguments and returns `(observation, extras)`.
    `observation` is sent back to the model as a tool message. `extras` is an
    arbitrary dict merged into the result metadata; if the finish tool returns
    `{"text": ..., "source_ids": [...]}`, those become the final answer.
    """
    result = ToolLoopResult()
    tool_count = 0

    while tool_count < max_tool_calls:
        start = time.perf_counter()
        response = client.chat(messages=messages, tools=tools, max_tokens=2048)
        result.usage = Usage(
            prompt_tokens=result.usage.prompt_tokens + response.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens + response.usage.completion_tokens,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        result.metadata.setdefault("latency_ms", 0.0)
        result.metadata["latency_ms"] += latency_ms

        messages.append(
            {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in response.tool_calls
                ]
                if response.tool_calls
                else [],
            }
        )

        if not response.tool_calls:
            # Model returned a plain text response; treat it as the answer.
            result.content = response.content or ""
            result.tool_calls = tool_count
            return result

        for tc in response.tool_calls:
            tool_count += 1
            handler = handlers.get(tc.name)
            if handler is None:
                observation = f"Unknown tool: {tc.name}."
            else:
                try:
                    observation, extras = handler(tc.arguments)
                except Exception as exc:
                    observation = f"Error: {exc}"
                    extras = {}

                if isinstance(extras, dict):
                    result.metadata.update(extras)
                    if tc.name == finish_tool:
                        result.content = extras.get("text", "")
                        result.source_ids = extras.get("source_ids", [])
                        result.tool_calls = tool_count
                        return result

            messages.append(build_tool_message(tc.id, observation))

    result.content = ""
    result.metadata["stopped_due_to"] = "max_tool_calls"
    result.tool_calls = tool_count
    return result


# ---------------------------------------------------------------------------
# Shared tool handlers
# ---------------------------------------------------------------------------


def answer_tool_handler(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Common handler for the final `answer` tool used by all brain systems."""
    return "Answer recorded.", {
        "text": args.get("text", ""),
        "source_ids": list(args.get("source_ids", [])),
    }
