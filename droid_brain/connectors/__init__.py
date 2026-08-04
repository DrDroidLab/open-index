"""Connectors: codified scripts that pull assets from an MCP server into the brain."""

from droid_brain.connectors.base import Connector, EntitySpec
from droid_brain.connectors.mcp_client import McpClient
from droid_brain.connectors.runner import run_connector

__all__ = ["Connector", "EntitySpec", "McpClient", "run_connector"]
