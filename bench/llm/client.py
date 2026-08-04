"""Thin OpenAI client wrapper with retry, usage capture, and a cost ledger."""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional

from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageToolCall

from bench.config import (
    AGENT_MODEL,
    DEFAULT_API_KEY,
    DEFAULT_ENDPOINT,
    TEMPERATURE,
)


# ---------------------------------------------------------------------------
# Pricing constants (per 1M tokens) as of 2026-08-04 per plan §10
# ---------------------------------------------------------------------------

_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


def _price_per_1m(model: str) -> tuple[float, float]:
    """Return (input_price, output_price) per 1M tokens for a known model.

    Falls back to gpt-4o-mini pricing for unknown models so cost accounting is
    never None, but warns via a zero-cost fallback is safer than crashing.
    """
    return _PRICING.get(model, _PRICING["gpt-4o-mini"])


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class ContentFilteredError(Exception):
    """Azure's content management policy rejected the prompt (HTTP 400).

    Distinct from other BadRequestErrors so callers can degrade gracefully
    (skip the offending evidence / question) instead of aborting the run.
    """


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""


@dataclass
class CostLedger:
    """Running tally of token usage and estimated cost."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_cost_usd: float = 0.0

    def add(self, usage: Usage, model: str) -> None:
        in_price, out_price = _price_per_1m(model)
        cost = (
            usage.prompt_tokens * in_price / 1_000_000
            + usage.completion_tokens * out_price / 1_000_000
        )
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_cost_usd += cost

    def usage(self) -> Usage:
        return Usage(self.prompt_tokens, self.completion_tokens)


# ---------------------------------------------------------------------------
# Real client
# ---------------------------------------------------------------------------


class LLMClient:
    """OpenAI-compatible chat client with retries, usage capture, and cost ledger."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = AGENT_MODEL,
        temperature: float = TEMPERATURE,
        max_retries: int = 6,
    ):
        self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.api_key = api_key or DEFAULT_API_KEY
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.ledger = CostLedger()
        self._client = OpenAI(base_url=self.endpoint, api_key=self.api_key)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 2048,
        seed: Optional[int] = None,
    ) -> ChatResponse:
        """Call the chat completions endpoint with exponential-backoff retries.

        Retries on rate limits (429) and server errors (5xx). Records token
        usage and updates the cumulative cost ledger. Forwards `seed` when
        provided for deterministic sampling.
        """
        temp = temperature if temperature is not None else self.temperature
        attempt = 0
        backoff = 1.0
        last_exception: Optional[Exception] = None
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,  # type: ignore[arg-type]
            "tools": tools,  # type: ignore[arg-type]
            "temperature": temp,
            "max_tokens": max_tokens,
        }
        if seed is not None:
            request_kwargs["seed"] = seed

        while attempt < self.max_retries:
            attempt += 1
            try:
                completion: ChatCompletion = self._client.chat.completions.create(
                    **request_kwargs
                )
                return self._parse_completion(completion)
            except Exception as exc:  # pragma: no cover - retry path exercised in tests
                last_exception = exc
                if self._is_content_filter(exc):
                    raise ContentFilteredError(str(exc)) from exc
                if not self._is_retryable(exc):
                    raise
                if attempt >= self.max_retries:
                    break
                time.sleep(backoff)
                backoff *= 2

        raise RuntimeError(
            f"LLM call failed after {self.max_retries} attempts: {last_exception}"
        ) from last_exception

    @staticmethod
    def _is_content_filter(exc: Exception) -> bool:
        """True when Azure's content management policy rejected the prompt (400)."""
        from openai import BadRequestError

        if not isinstance(exc, BadRequestError):
            return False
        msg = str(exc).lower()
        return (
            "content management policy" in msg
            or "responsibleaipolicyviolation" in msg
            or "content_filter" in msg
        )

    def _is_retryable(self, exc: Exception) -> bool:
        """Return True for 429/5xx and transient network errors."""
        from openai import APIConnectionError, APIStatusError, RateLimitError

        if isinstance(exc, RateLimitError):
            return True
        if isinstance(exc, APIConnectionError):
            return True
        if isinstance(exc, APIStatusError):
            return exc.status_code >= 500 or exc.status_code == 429
        return False

    def _parse_completion(self, completion: ChatCompletion) -> ChatResponse:
        choice = completion.choices[0]
        message = choice.message
        usage = completion.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        response_usage = Usage(prompt_tokens, completion_tokens)
        self.ledger.add(response_usage, self.model)

        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(self._parse_tool_call(tc))

        return ChatResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            usage=response_usage,
            model=self.model,
        )

    def _parse_tool_call(self, tc: ChatCompletionMessageToolCall) -> ToolCall:
        args: dict[str, Any] = {}
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {"raw_arguments": tc.function.arguments}
        return ToolCall(id=tc.id, name=tc.function.name, arguments=args)


# ---------------------------------------------------------------------------
# Deterministic fake client for unit tests
# ---------------------------------------------------------------------------


class _ErrorResponse:
    """Queue slot that raises instead of returning a ChatResponse."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc


class FakeLLMClient:
    """Scripted LLM client that replays queued responses."""

    def __init__(self, model: str = AGENT_MODEL, temperature: float = TEMPERATURE):
        self.model = model
        self.temperature = temperature
        self.ledger = CostLedger()
        self._responses: deque[ChatResponse | _ErrorResponse] = deque()
        self._calls: list[list[dict[str, Any]]] = []
        self._max_tokens: list[int] = []
        self._seeds: list[Optional[int]] = []

    def queue(self, response: ChatResponse) -> "FakeLLMClient":
        self._responses.append(response)
        return self

    def queue_text(self, content: str, usage: Usage | None = None) -> "FakeLLMClient":
        self._responses.append(
            ChatResponse(content=content, usage=usage or Usage(10, 5), model=self.model)
        )
        return self

    def queue_error(self, exc: Exception) -> "FakeLLMClient":
        """Queue an exception to be raised on the call that consumes this slot."""
        self._responses.append(_ErrorResponse(exc))
        return self

    def queue_tool_calls(
        self, calls: list[tuple[str, dict[str, Any]]], usage: Usage | None = None
    ) -> "FakeLLMClient":
        tool_calls = [
            ToolCall(id=f"call_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(calls)
        ]
        self._responses.append(
            ChatResponse(
                content="",
                tool_calls=tool_calls,
                usage=usage or Usage(10, 5),
                model=self.model,
            )
        )
        return self

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 2048,
        seed: Optional[int] = None,
    ) -> ChatResponse:
        if not self._responses:
            raise RuntimeError("FakeLLMClient response queue is empty")
        self._calls.append([dict(m) for m in messages])
        self._max_tokens.append(max_tokens)
        self._seeds.append(seed)
        response = self._responses.popleft()
        if isinstance(response, _ErrorResponse):
            raise response.exc
        self.ledger.add(response.usage, self.model)
        return response

    @property
    def calls(self) -> list[list[dict[str, Any]]]:
        return self._calls

    @property
    def seeds(self) -> list[Optional[int]]:
        return self._seeds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_tool_message(tool_call_id: str, content: Any) -> dict[str, Any]:
    """Build a tool-role message from a handler result."""
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": str(content) if content is not None else "",
    }


def message_text(messages: Iterable[dict[str, Any]]) -> str:
    """Concatenate message contents for logging/prompt inspection."""
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if content:
            parts.append(str(content))
    return "\n".join(parts)


def tiktoken_count(text: str, model: str = "cl100k_base") -> int:
    """Return a token count using tiktoken; falls back to a rough estimate."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding(model)
        return len(enc.encode(text))
    except Exception:  # pragma: no cover - tiktoken is installed in this env
        return len(text) // 4
