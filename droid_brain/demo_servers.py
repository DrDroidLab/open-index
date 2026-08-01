"""Fake MCP servers for trying out extraction without real credentials.

Run one with: python -m droid_brain.demo_servers <grafana|github|aws>

Each serves a small static dataset over stdio, mimicking the real tools'
list-* APIs. Payloads are deliberately nested so extracted entities exercise
the brain's nested-JSON support.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

GRAFANA_DASHBOARDS = [
    {
        "title": "api-gateway-overview",
        "uid": "api-gw-ov",
        "url": "https://grafana.example.com/d/api-gw-ov",
        "folder": "Platform",
        "owner": "platform",
        "panels": [
            {"title": "Request rate", "queries": ["sum(rate(nginx_http_requests_total[5m]))"]},
            {"title": "p99 latency", "queries": ["histogram_quantile(0.99, rate(nginx_http_request_duration_seconds_bucket[5m]))"]},
            {"title": "5xx rate", "queries": ["sum(rate(nginx_http_requests_total{status=~'5..'}[5m]))"]},
        ],
    },
    {
        "title": "postgres-payments-health",
        "uid": "pg-pay-hl",
        "url": "https://grafana.example.com/d/pg-pay-hl",
        "folder": "Payments",
        "owner": "payments",
        "panels": [
            {"title": "Connections", "queries": ["pg_stat_activity_count{db='payments'}"]},
            {"title": "Replication lag", "queries": ["pg_replication_lag_seconds{db='payments'}"]},
        ],
    },
]

GITHUB_REPOSITORIES = [
    {
        "name": "api-gateway",
        "full_name": "acme/api-gateway",
        "description": "Edge ingress for all external API traffic",
        "language": "Go",
        "team": "platform",
        "url": "https://github.com/acme/api-gateway",
        "topics": ["ingress", "auth", "critical"],
        "ci": {"workflow": "build-and-deploy.yaml", "status": "passing"},
    },
    {
        "name": "payments-service",
        "full_name": "acme/payments-service",
        "description": "Card payments and refunds via Stripe",
        "language": "Java",
        "team": "payments",
        "url": "https://github.com/acme/payments-service",
        "topics": ["payments", "pci", "critical"],
        "ci": {"workflow": "gradle.yml", "status": "passing"},
    },
    {
        "name": "user-service",
        "full_name": "acme/user-service",
        "description": "User accounts, profiles and sessions",
        "language": "Python",
        "team": "platform",
        "url": "https://github.com/acme/user-service",
        "topics": ["auth", "users"],
        "ci": {"workflow": "pytest.yml", "status": "failing"},
    },
]

AWS_DATABASES = [
    {
        "identifier": "postgres-payments",
        "engine": "postgres",
        "version": "15.4",
        "region": "us-east-1",
        "endpoint": {"host": "postgres-payments.abc123.us-east-1.rds.amazonaws.com", "port": 5432},
        "size": "db.r6g.large",
        "replicas": [{"identifier": "postgres-payments-ro", "role": "reader"}],
    },
    {
        "identifier": "redis-sessions",
        "engine": "redis",
        "version": "7.1",
        "region": "us-east-1",
        "endpoint": {"host": "redis-sessions.def456.cache.amazonaws.com", "port": 6379},
        "size": "cache.r6g.large",
        "replicas": [],
    },
]

_TOOLS: dict[str, dict[str, tuple[Tool, Any]]] = {
    "grafana": {
        "list_dashboards": (
            Tool(
                name="list_dashboards",
                description="List all Grafana dashboards with their panels and queries.",
                input_schema={"type": "object", "properties": {}},
            ),
            lambda: GRAFANA_DASHBOARDS,
        ),
    },
    "github": {
        "list_repositories": (
            Tool(
                name="list_repositories",
                description="List repositories in the organisation with metadata and CI status.",
                input_schema={"type": "object", "properties": {}},
            ),
            lambda: GITHUB_REPOSITORIES,
        ),
    },
    "aws": {
        "list_databases": (
            Tool(
                name="list_databases",
                description="List RDS/ElastiCache database instances with endpoints and replicas.",
                input_schema={"type": "object", "properties": {}},
            ),
            lambda: AWS_DATABASES,
        ),
    },
}


def serve(vendor: str) -> None:
    if vendor not in _TOOLS:
        raise ValueError(f"unknown fake server {vendor!r}: choose from {', '.join(_TOOLS)}")
    tools = _TOOLS[vendor]

    async def _on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
        return ListToolsResult(tools=[t for t, _ in tools.values()])

    async def _on_call_tool(ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        entry = tools.get(params.name)
        if not entry:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Unknown tool {params.name!r}")],
                is_error=True,
            )
        _, handler = entry
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(handler()))]
        )

    server = Server(
        name=f"fake-{vendor}",
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_main())


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in _TOOLS:
        print(f"usage: python -m droid_brain.demo_servers <{'|'.join(_TOOLS)}>", file=sys.stderr)
        sys.exit(1)
    serve(sys.argv[1])
