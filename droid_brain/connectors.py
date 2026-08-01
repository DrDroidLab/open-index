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
import os
import subprocess
import sys
from typing import Any, Optional

from mcp.types import CallToolResult, TextContent

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


async def extract_assets(connector: Connector) -> dict[str, Any]:
    """Run a connector: call MCP tool → transform → write entities to brain.

    Returns a summary dict with counts and created entity IDs.
    """
    # 1. Call the MCP tool
    raw_result = await _call_mcp_tool(
        connector.mcp_command,
        connector.tool_name,
        connector.tool_arguments,
    )

    # 2. Parse result (expect JSON array of objects)
    items = _parse_items(raw_result, connector.transform)

    # 3. Write to brain
    db = DroidBrain()
    entities = []

    # Ensure brain and doc_type exist
    brains = {b["name"] for b in db.list_brains()}
    if connector.brain_name not in brains:
        db.create_brain(connector.brain_name, f"Auto-created by connector '{connector.name}'")

    existing_dt = db.get_doctype(connector.brain_name, connector.doc_type)
    if not existing_dt:
        # Infer schema from first item
        schema = _infer_schema(items[0]) if items else []
        db.create_doctype(connector.brain_name, connector.doc_type,
                          f"Auto-created by connector '{connector.name}'",
                          [s.model_dump() for s in schema])

    for item in items:
        mapped_data = {connector.field_mapping.get(k, k): v
                       for k, v in item.items()}
        entity = db.create_entity(connector.brain_name, connector.doc_type, mapped_data)
        entities.append(entity)

    return {
        "connector": connector.name,
        "brain": connector.brain_name,
        "doc_type": connector.doc_type,
        "entities_created": len(entities),
        "entity_ids": [e["entity_id"] for e in entities],
    }


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


def _parse_items(raw: str, transform: Optional[str]) -> list[dict]:
    """Parse raw MCP output into a list of dicts. Applies optional transform."""
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
        namespace = {"item": None, "result": []}
        for item in data:
            namespace["item"] = item
            exec(f"result.append({transform})", namespace)
        data = namespace["result"]

    return data


async def _call_mcp_tool(
    mcp_command: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: int = 30,
) -> str:
    """Call an MCP tool via subprocess (stdio transport).

    Sends an MCP JSON-RPC initialize + tools/call message to the MCP server.
    """
    proc = await asyncio.create_subprocess_exec(
        *mcp_command.split(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # MCP initialize handshake
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

    # Initialized notification
    initialized_msg = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }) + "\n"

    # Tools/call request
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

    # Parse MCP response — look for the tools/call response (id=2)
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
    await asyncio.sleep(0.5)  # give server time to process init

    proc.stdin.write(call_msg.encode())
    await proc.stdin.drain()

    assert proc.stdout is not None
    stdout = await proc.stdout.read()
    stderr = await proc.stderr.read() if proc.stderr else b""
    return stdout, stderr


def run_connector(connector: Connector) -> dict[str, Any]:
    """Synchronous wrapper for extract_assets."""
    return asyncio.run(extract_assets(connector))


# ---------------------------------------------------------------------------
# Direct-mode extraction (for testing / embedded MCP handlers)
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

    db = DroidBrain()
    entities = []
    for item in items:
        mapped_data = {field_mapping.get(k, k): v for k, v in item.items()}
        entity = db.create_entity(brain_name, doc_type, mapped_data)
        entities.append(entity)

    return {
        "brain": brain_name,
        "doc_type": doc_type,
        "entities_created": len(entities),
        "entity_ids": [e["entity_id"] for e in entities],
    }
