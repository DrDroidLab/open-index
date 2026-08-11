"""The id that ties one agent turn to the retrievals it caused.

An agent asks a question, the index answers with documents, and the agent then
does something surprising. Working out why means knowing *which* retrieval fed
that turn — so every recorded fetch carries a trace id, and looking one up
returns the queries, the documents, and the scores behind them.

The id is ambient rather than a parameter on every method: it enters once, at
the edge, and `Brain` reads it wherever it records. A ContextVar is the right
shape for that — it is per-task and per-thread, so two concurrent requests never
see each other's id, which a module global would not survive.

Two edges set it:

    HTTP   an `X-Trace-Id` request header, via `trace_from_headers`
    MCP    an explicit `trace_id` tool argument, because an MCP tool has no
           ambient request to read

Nothing generates one. An absent trace id is recorded as NULL and everything
still works; inventing one would create ids that correlate nothing, which is
worse than none at all.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_current: ContextVar[Optional[str]] = ContextVar("open_index_trace_id", default=None)

# Trace ids arrive from outside and are stored and displayed, so they are bounded
# and restricted to characters that cannot break out of a log line or a page.
# Anything else is dropped rather than sanitised: a mangled id would silently
# fail to correlate with whatever the caller thinks it sent.
_VALID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

TRACE_HEADER = "x-trace-id"


def normalize(value: Optional[str]) -> Optional[str]:
    """A usable trace id, or None. Never raises — a bad id must not fail a read."""
    if not value:
        return None
    value = value.strip()
    return value if _VALID.match(value) else None


def current_trace_id() -> Optional[str]:
    return _current.get()


def set_trace_id(value: Optional[str]) -> None:
    _current.set(normalize(value))


@contextmanager
def trace(value: Optional[str]) -> Iterator[Optional[str]]:
    """Bind a trace id for the duration of a block, then restore the previous.

    Restoring matters on a server: the worker outlives the request, and a leaked
    id would attribute the next caller's retrievals to the previous one.
    """
    token = _current.set(normalize(value))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def trace_from_headers(headers) -> Optional[str]:
    """Read the trace id from request headers (case-insensitive)."""
    try:
        return normalize(headers.get(TRACE_HEADER))
    except Exception:
        return None
