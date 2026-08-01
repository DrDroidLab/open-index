"""Fake Infrastructure MCP server — returns mock services and dashboards.

Run: python examples/infra_mcp.py
"""

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

MOCK_SERVICES = [
    {
        "name": "api-gateway",
        "tier": "tier-0",
        "team": "platform",
        "p99_latency_ms": 45,
        "error_rate_pct": 0.02,
        "instance_count": 12,
        "dependencies": ["user-service", "auth-service"],
    },
    {
        "name": "user-service",
        "tier": "tier-1",
        "team": "backend",
        "p99_latency_ms": 120,
        "error_rate_pct": 0.15,
        "instance_count": 6,
        "dependencies": ["postgres-primary", "redis-cache"],
    },
    {
        "name": "payment-worker",
        "tier": "tier-1",
        "team": "payments",
        "p99_latency_ms": 2500,
        "error_rate_pct": 0.8,
        "instance_count": 4,
        "dependencies": ["kafka-cluster", "stripe-api"],
    },
    {
        "name": "inventory-db",
        "tier": "tier-0",
        "team": "data-platform",
        "p99_latency_ms": 15,
        "error_rate_pct": 0.0,
        "instance_count": 3,
        "dependencies": [],
    },
]

MOCK_DASHBOARDS = [
    {
        "title": "API Gateway Overview",
        "url": "https://grafana.acme.com/d/api-overview",
        "service": "api-gateway",
        "panels": ["Request Rate", "Latency P50/P99", "Error Rate", "Upstream Health"],
    },
    {
        "title": "Payment Processing Monitor",
        "url": "https://grafana.acme.com/d/payments",
        "service": "payment-worker",
        "panels": ["Consumer Lag", "Stripe Latency", "Success Rate", "Dead Letters"],
    },
    {
        "title": "DB Health Dashboard",
        "url": "https://grafana.acme.com/d/db-health",
        "service": "inventory-db",
        "panels": ["QPS", "Replication Lag", "Connection Pool", "Disk Usage"],
    },
]

TOOLS = [
    Tool(
        name="list_services",
        title="List Services",
        description="List infrastructure services with health metrics",
        input_schema={
            "type": "object",
            "properties": {
                "tier": {"type": "string", "description": "Filter by tier (tier-0, tier-1, etc.)"},
                "team": {"type": "string", "description": "Filter by team"},
            },
        },
    ),
    Tool(
        name="list_dashboards",
        title="List Dashboards",
        description="List monitoring dashboards",
        input_schema={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Filter by service name"},
            },
        },
    ),
]


async def list_services(arguments: dict) -> str:
    tier = arguments.get("tier")
    team = arguments.get("team")
    results = MOCK_SERVICES
    if tier:
        results = [s for s in results if s["tier"] == tier]
    if team:
        results = [s for s in results if s["team"] == team]
    return json.dumps(results)


async def list_dashboards(arguments: dict) -> str:
    service = arguments.get("service")
    results = MOCK_DASHBOARDS
    if service:
        results = [d for d in results if d["service"] == service]
    return json.dumps(results)


HANDLERS = {
    "list_services": list_services,
    "list_dashboards": list_dashboards,
}


async def on_list_tools(request, params):
    return ListToolsResult(tools=TOOLS)


async def on_call_tool(request, params: CallToolRequestParams):
    handler = HANDLERS.get(params.name)
    if handler:
        text = await handler(params.arguments or {})
    else:
        text = f"Unknown tool: {params.name}"
    return CallToolResult(content=[TextContent(type="text", text=text)])


server = Server(
    name="fake-infra",
    version="0.1.0",
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
