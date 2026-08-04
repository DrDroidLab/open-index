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
    require_finish_tool: bool = False,
    seed: Optional[int] = None,
) -> ToolLoopResult:
    """Run a tool-calling loop until `finish_tool` is called or the budget is exhausted.

    Each handler receives the tool arguments and returns `(observation, extras)`.
    `observation` is sent back to the model as a tool message. `extras` is an
    arbitrary dict merged into the result metadata; if the finish tool returns
    `{"text": ..., "source_ids": [...]}`, those become the final answer.

    When `require_finish_tool` is True and `finish_tool` is "answer", a plain-text
    response is not accepted: the loop sends a corrective message and continues.
    Plain text is only accepted as a fallback when the round budget is exhausted,
    and `result.metadata["fallback"] = True` is recorded.
    """
    result = ToolLoopResult()
    tool_count = 0
    rounds = 0

    while rounds < max_tool_calls:
        rounds += 1
        start = time.perf_counter()
        response = client.chat(messages=messages, tools=tools, max_tokens=2048, seed=seed)
        result.usage = Usage(
            prompt_tokens=result.usage.prompt_tokens + response.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens + response.usage.completion_tokens,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        result.metadata.setdefault("latency_ms", 0.0)
        result.metadata["latency_ms"] += latency_ms

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
        if response.tool_calls:
            # Azure rejects "tool_calls": [] with a 400 — omit the key entirely
            # when the model returned no tool calls.
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in response.tool_calls
            ]
        messages.append(assistant_msg)

        if not response.tool_calls:
            if require_finish_tool and finish_tool == "answer" and rounds < max_tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You must call the `answer` tool with your final answer; "
                            "plain text is not accepted."
                        ),
                    }
                )
                continue

            # Plain-text acceptance: for ingest this is treated as completion; for
            # answer it is a last-resort fallback when rounds are exhausted.
            result.content = response.content or ""
            result.tool_calls = tool_count
            if require_finish_tool and finish_tool == "answer":
                result.metadata["fallback"] = True
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
