"""Remote brain access — query a brain running on another machine via MCP.

Set DROID_BRAIN_REMOTE_URL=http://host:8000 to use a remote brain.
Uses the MCP Streamable HTTP client to connect to a remote MCP server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from droid_brain.models import BrainStructure


class RemoteDroidBrain:
    """DroidBrain-compatible client that connects to a remote MCP server.

    The remote MCP server must be running in Streamable HTTP mode:
      droid-brain mcp-server --transport sse --port 8000

    Remote URL should be the base URL (e.g. http://host:8000). The client
    appends /mcp for the MCP endpoint.
    """

    def __init__(self, remote_url: str):
        self.remote_url = remote_url.rstrip("/")
        self._mcp_url = f"{self.remote_url}/mcp"

    def _run_async(self, coro):
        """Run an async coroutine synchronously, managing the event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result(timeout=30)
        else:
            return asyncio.run(coro)

    async def _call_tool_async(self, tool_name: str, arguments: dict[str, Any]) -> str:
        from mcp.client.streamable_http import streamable_http_client
        from mcp.client.session import ClientSession

        async with streamable_http_client(self._mcp_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                contents = getattr(result, "content", [])
                if contents:
                    return contents[0].text if hasattr(contents[0], "text") else str(contents[0])
                return ""

    def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        return self._run_async(
            self._call_tool_async(tool_name, arguments)
        )

    def list_brains(self) -> list[dict]:
        text = self._call_tool("list_brains", {})
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return []

    def get_brain_structure(self, brain_name: str) -> BrainStructure:
        text = self._call_tool("brain_structure", {"brain_name": brain_name})
        return _parse_brain_structure(brain_name, text)

    def search(
        self,
        brain_name: str,
        query_text: str,
        doc_type: Optional[str] = None,
        size: int = 20,
        boost: Optional[dict[str, float]] = None,  # accepted for interface compatibility; remote tool has no boost support
    ) -> list[dict]:
        args = {"brain_name": brain_name, "query": query_text, "max_results": size}
        if doc_type:
            args["doc_type"] = doc_type
        text = self._call_tool("search_brain", args)
        if text == "No results found.":
            return []
        return _parse_search_results(text)

    def get_entity(self, brain_name: str, entity_id: str) -> Optional[dict]:
        text = self._call_tool(
            "fetch_entity", {"brain_name": brain_name, "entity_id": entity_id}
        )
        if "not found" in text.lower():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def create_brain(self, name: str, description: str = "") -> dict:
        raise NotImplementedError("Remote brain creation not yet supported via MCP.")

    def delete_brain(self, name: str) -> None:
        raise NotImplementedError("Remote brain deletion not yet supported via MCP.")

    def list_doctypes(self, brain_name: str) -> list[dict]:
        raise NotImplementedError("Use get_brain_structure() for remote schema info.")

    def create_entity(self, brain_name: str, doc_type: str, data: dict[str, Any]) -> dict:
        raise NotImplementedError("Remote entity creation not yet supported via MCP.")

    def list_entities(self, brain_name: str, doc_type: Optional[str] = None, size: int = 50) -> list[dict]:
        raise NotImplementedError("Use search() for remote entity listing.")


def _parse_search_results(text: str) -> list[dict]:
    """Parse search_brain tool output back into entity dicts."""
    results = []
    current = {}
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("--- Result"):
            if current:
                results.append(current)
                current = {}
        elif line.startswith("entity_id:"):
            current["entity_id"] = line.split(":", 1)[1].strip()
        elif line.startswith("doc_type:"):
            current["doc_type"] = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_str = line.split(":", 1)[1].strip()
            try:
                current["data"] = json.loads(data_str)
            except json.JSONDecodeError:
                current["data"] = {}
    if current:
        results.append(current)
    return results


def _parse_brain_structure(brain_name: str, text: str) -> BrainStructure:
    """Parse brain_structure tool output back into BrainStructure.

    Handles the full output format: doc_type names, entity counts, descriptions,
    schema fields, and example entities.
    """
    structure = BrainStructure(brain_name=brain_name)
    current_dt: dict | None = None
    in_fields = False

    for line in text.split("\n"):
        stripped = line.strip()

        # New doc_type section: "📁 service (5 entities)"
        if stripped.startswith("📁"):
            if current_dt:
                structure.doc_types.append(current_dt)
            parts = stripped.split("(")
            dt_name = parts[0].replace("📁", "").strip()
            count_str = parts[1].split()[0] if len(parts) > 1 else "0"
            try:
                count = int(count_str)
            except ValueError:
                count = 0
            current_dt = {"name": dt_name, "entity_count": count,
                          "description": "", "examples": [], "schema_fields": []}
            structure.total_entities += count
            in_fields = False

        elif current_dt is not None:
            if stripped.startswith("Description:"):
                current_dt["description"] = stripped.split(":", 1)[1].strip()
                in_fields = False
            elif stripped == "Fields:":
                in_fields = True
            elif stripped == "Examples:":
                in_fields = False
            elif in_fields and stripped.startswith("•"):
                # Field line: "   • name : string (required, search=syntactic)"
                field_str = stripped.lstrip("•").strip()
                field_parts = field_str.split(":", 1)
                field_name = field_parts[0].strip()
                field_type = "string"
                meta = ""
                if len(field_parts) > 1:
                    rest = field_parts[1].strip()
                    # rest could be "string (required, search=syntactic)" or just "string"
                    type_and_meta = rest.split("(", 1)
                    field_type = type_and_meta[0].strip()
                    if len(type_and_meta) > 1:
                        meta = type_and_meta[1].rstrip(")")
                current_dt["schema_fields"].append({
                    "name": field_name,
                    "field_type": field_type,
                })
            elif not in_fields and (stripped.startswith("{") or stripped.startswith("[")):
                # Example entity JSON
                try:
                    current_dt["examples"].append(json.loads(stripped))
                except json.JSONDecodeError:
                    pass

    if current_dt:
        structure.doc_types.append(current_dt)
    return structure
