"""Connector framework — extract assets from MCP servers into a Droid Brain.

Flow:
  1. Define a Connector (MCP server, tool name, brain/doc_type, field mapping)
  2. Call extract_assets(connector) — it connects to the MCP server, calls the tool,
     transforms results via the mapping, and creates entities in the brain.

Supports both real MCP servers (subprocess) and direct Python handler calls (for testing).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from droid_brain.core import DroidBrain
from droid_brain.models import SchemaField


class Connector:
    """Describes how to extract data from an MCP server into a brain."""

    def __init__(
        self,
        name: str,
        mcp_command: str,              # e.g. "python examples/github_mcp.py"
        tool_name: str,                # tool to call on the MCP server
        brain_name: str,               # target brain
        doc_type: str,                 # doc_type to create entities as
        field_mapping: dict[str, str], # tool_result_field → entity_data_field
        tool_arguments: Optional[dict[str, Any]] = None,
        transform: Optional[str] = None,  # optional Python expression to transform results
    ):
        self.name = name
        self.mcp_command = mcp_command
        self.tool_name = tool_name
        self.brain_name = brain_name
        self.doc_type = doc_type
        self.field_mapping = field_mapping
        self.tool_arguments = tool_arguments or {}
        self.transform = transform


# ---------------------------------------------------------------------------
# Shared persistence — used by both subprocess and in-process paths
# ---------------------------------------------------------------------------


def _persist_items(
    brain_name: str,
    doc_type: str,
    field_mapping: dict[str, str],
    items: list[dict],
    connector_name: Optional[str] = None,
) -> dict[str, Any]:
    """Ensure brain/doc_type exist, then create entities.

    Returns a summary dict with counts and created entity IDs.
    """
    db = DroidBrain()

    # Auto-create brain if missing
    brains = {b["name"] for b in db.list_brains()}
    if brain_name not in brains:
        label = f"Auto-created by connector '{connector_name}'" if connector_name else "Auto-created by connector"
        db.create_brain(brain_name, label)

    # Auto-create doc_type (infer schema from first item) if missing
    existing_dt = db.get_doctype(brain_name, doc_type)
    if not existing_dt:
        schema = _infer_schema(items[0]) if items else []
        label = f"Auto-created by connector '{connector_name}'" if connector_name else "Auto-created by connector"
        db.create_doctype(brain_name, doc_type, label,
                          [s.model_dump() for s in schema])

    entities = []
    for item in items:
        mapped_data = {field_mapping.get(k, k): v for k, v in item.items()}
        entity = db.create_entity(brain_name, doc_type, mapped_data)
        entities.append(entity)

    result: dict[str, Any] = {
        "brain": brain_name,
        "doc_type": doc_type,
        "entities_created": len(entities),
        "entity_ids": [e["entity_id"] for e in entities],
    }
    if connector_name:
        result["connector"] = connector_name
    return result


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------


def _infer_schema(item: dict) -> list[SchemaField]:
    """Infer a basic schema from a JSON object."""
    fields = []
    for key, value in item.items():
        if isinstance(value, bool):
            ft = "boolean"
        elif isinstance(value, int):
            ft = "number"
        elif isinstance(value, dict):
            ft = "object"
        elif isinstance(value, list):
            ft = "array"
        else:
            ft = "string"
        fields.append(SchemaField(name=key, field_type=ft))
    return fields


# ---------------------------------------------------------------------------
# Item parsing & transform
# ---------------------------------------------------------------------------


def _parse_items(raw: str, transform: Optional[str]) -> list[dict]:
    """Parse raw MCP output into a list of dicts. Applies optional transform.

    NOTE: the ``transform`` parameter is a Python expression that is exec'd
    inside a restricted namespace per item. Only use with trusted input.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = raw

    if isinstance(data, dict):
        # Try common wrapping keys
        for key in ("items", "results", "data", "records"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        else:
            # If dict but no list key, wrap as single-item list
            data = [data]

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array from MCP tool, got {type(data).__name__}")

    if transform:
        namespace: dict[str, Any] = {"item": None, "result": []}
        for item in data:
            namespace["item"] = item
            # Security note: exec'ing user-supplied code. This is a local CLI
            # tool — only trusted users should supply --transform expressions.
            exec(f"result.append({transform})", {"__builtins__": {}}, namespace)
        data = namespace["result"]

    return data


# ---------------------------------------------------------------------------
# Subprocess-based MCP extraction
# ---------------------------------------------------------------------------


async def extract_assets(connector: Connector) -> dict[str, Any]:
    """Run a connector: call MCP tool → transform → write entities to brain."""
    raw_result = await _call_mcp_tool(
        connector.mcp_command,
        connector.tool_name,
        connector.tool_arguments,
    )
    items = _parse_items(raw_result, connector.transform)
    return _persist_items(
        brain_name=connector.brain_name,
        doc_type=connector.doc_type,
        field_mapping=connector.field_mapping,
        items=items,
        connector_name=connector.name,
    )


async def _call_mcp_tool(
    mcp_command: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: int = 30,
) -> str:
    """Call an MCP tool via subprocess (stdio transport)."""
    proc = await asyncio.create_subprocess_exec(
        *mcp_command.split(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    init_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "droid-brain-connector", "version": "0.1.0"},
        },
    }) + "\n"

    initialized_msg = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }) + "\n"

    call_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }) + "\n"

    try:
        stdout, stderr = await asyncio.wait_for(
            _send_and_receive(proc, init_msg, initialized_msg, call_msg),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"MCP tool '{tool_name}' timed out after {timeout}s")
    finally:
        if proc.returncode is None:
            proc.kill()

    for line in stdout.decode().strip().split("\n"):
        try:
            msg = json.loads(line)
            if msg.get("id") == 2:
                result = msg.get("result", {})
                content = result.get("content", [])
                if content and isinstance(content, list):
                    return content[0].get("text", "")
                return json.dumps(result)
        except json.JSONDecodeError:
            continue

    return "[]"


async def _send_and_receive(
    proc: asyncio.subprocess.Process,
    init_msg: str,
    initialized_msg: str,
    call_msg: str,
) -> tuple[bytes, bytes]:
    """Send MCP handshake + tool call, read responses."""
    assert proc.stdin is not None
    proc.stdin.write(init_msg.encode())
    proc.stdin.write(initialized_msg.encode())
    await proc.stdin.drain()
    await asyncio.sleep(0.5)

    proc.stdin.write(call_msg.encode())
    await proc.stdin.drain()

    assert proc.stdout is not None
    stdout = await proc.stdout.read()
    stderr = await proc.stderr.read() if proc.stderr else b""
    return stdout, stderr


# ---------------------------------------------------------------------------
# In-process extraction (for testing / embedded MCP handlers)
# ---------------------------------------------------------------------------


async def extract_from_handlers(
    tool_handlers: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    brain_name: str,
    doc_type: str,
    field_mapping: dict[str, str],
    transform: Optional[str] = None,
) -> dict[str, Any]:
    """Extract assets by calling MCP handlers directly (no subprocess).

    Useful for testing or when the MCP server is in-process.
    tool_handlers maps tool name → async handler function.
    """
    handler = tool_handlers.get(tool_name)
    if not handler:
        raise ValueError(f"Tool '{tool_name}' not found in handlers")

    raw_result = await handler(arguments)
    items = _parse_items(raw_result, transform)
    return _persist_items(
        brain_name=brain_name,
        doc_type=doc_type,
        field_mapping=field_mapping,
        items=items,
    )
