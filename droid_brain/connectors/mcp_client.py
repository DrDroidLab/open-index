"""A minimal MCP client: JSON-RPC 2.0 over streamable HTTP (with SSE responses).

Ported down from the reference `mcp_server_api_processor.py` to just what a
connector needs: initialize a session, `list_tools()`, and `call_tool()`. Auth is
passed as plain headers (e.g. {"Authorization": "Bearer ..."}). Transport is
detected from the URL: a path ending in `/sse` uses the SSE endpoint handshake;
everything else uses the modern streamable-HTTP transport.

This deliberately depends only on httpx (no MCP SDK) so connectors stay light.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import httpx

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "droid-brain-connector", "version": "0.1.0"}


class McpError(RuntimeError):
    pass


class McpClient:
    def __init__(
        self,
        base_url: str,
        auth_headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_headers = auth_headers or {}
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self._http = httpx.Client(timeout=timeout)
        self._initialized = False

    # -- transport -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Streamable HTTP servers may reply with either JSON or an SSE stream.
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self.auth_headers)
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        """Parse a JSON-RPC response that may be raw JSON or an SSE event body."""
        content_type = response.headers.get("content-type", "")
        text = response.text
        if "text/event-stream" in content_type or text.lstrip().startswith("event:"):
            # Pull the JSON payload out of the last `data:` line.
            data_payload = None
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data_payload = line[len("data:"):].strip()
            if data_payload is None:
                raise McpError("no data in SSE response")
            return json.loads(data_payload)
        return response.json()

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": str(uuid.uuid4()),
        }
        response = self._http.post(self.base_url, json=payload, headers=self._headers())
        # Capture a session id if the server issued one on initialize.
        session_from_header = response.headers.get("mcp-session-id")
        if session_from_header:
            self._session_id = session_from_header
        response.raise_for_status()
        body = self._parse_response(response)
        if "error" in body and body["error"]:
            raise McpError(f"{method} failed: {body['error']}")
        return body.get("result", {})

    # -- lifecycle -------------------------------------------------------------

    def initialize(self) -> None:
        if self._initialized:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": _CLIENT_INFO,
            },
        )
        # Best-effort notification; some servers require it before tool calls.
        try:
            self._http.post(
                self.base_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=self._headers(),
            )
        except httpx.HTTPError:
            pass
        self._initialized = True

    # -- tools -----------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        result = self._rpc("tools/list")
        tools = result.get("tools", [])
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema", {}),
            }
            for t in tools
        ]

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict[str, Any]:
        """Call a tool and return {content, is_error}. `content` is the raw MCP
        content list; use `extract_json` to turn it into records."""
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return {
            "content": result.get("content", []),
            "is_error": result.get("isError", False),
        }

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def extract_json(content: list[dict[str, Any]]) -> list[Any]:
    """Flatten an MCP tool-call `content` list into JSON records.

    Each text item is JSON-parsed; a parsed list is spread, a dict appended, and
    unparseable text kept as {'text': ...}. Mirrors the reference extractor's
    content-parsing loop."""
    records: list[Any] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text", "")
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                records.append({"text": text})
                continue
            if isinstance(parsed, list):
                records.extend(parsed)
            else:
                records.append(parsed)
    return records
