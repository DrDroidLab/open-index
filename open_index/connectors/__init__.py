"""Connectors: codified scripts that pull assets from an MCP server into the brain."""

from open_index.connectors.base import Connector, EntitySpec
from open_index.connectors.mcp_client import McpClient
from open_index.connectors.runner import run_connector

__all__ = ["Connector", "EntitySpec", "McpClient", "run_connector"]
