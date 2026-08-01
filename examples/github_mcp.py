"""Fake GitHub MCP server — returns mock repository data.

Run: python examples/github_mcp.py
"""

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

MOCK_REPOS = [
    {
        "name": "api-gateway",
        "org": "acme",
        "language": "Go",
        "stars": 142,
        "last_push": "2026-07-28",
        "description": "Main API gateway handling all external traffic",
        "deployment": "kubernetes",
        "team": "platform",
    },
    {
        "name": "user-service",
        "org": "acme",
        "language": "Python",
        "stars": 89,
        "last_push": "2026-07-30",
        "description": "User management and authentication service",
        "deployment": "kubernetes",
        "team": "backend",
    },
    {
        "name": "payment-worker",
        "org": "acme",
        "language": "Java",
        "stars": 67,
        "last_push": "2026-07-25",
        "description": "Async payment processing worker",
        "deployment": "kubernetes",
        "team": "payments",
    },
    {
        "name": "droid-brain",
        "org": "DrDroidLab",
        "language": "Python",
        "stars": 12,
        "last_push": "2026-08-01",
        "description": "Structured organisational knowledge for AI agents",
        "deployment": "docker",
        "team": "platform",
    },
    {
        "name": "landing-page",
        "org": "DrDroidLab",
        "language": "TypeScript",
        "stars": 5,
        "last_push": "2026-07-20",
        "description": "Marketing site for DrDroid",
        "deployment": "vercel",
        "team": "frontend",
    },
]

TOOLS = [
    Tool(
        name="list_repos",
        title="List Repositories",
        description="List all repositories with metadata (name, org, language, stars, etc.)",
        input_schema={
            "type": "object",
            "properties": {
                "org": {"type": "string", "description": "Filter by organisation"},
                "language": {"type": "string", "description": "Filter by language"},
            },
        },
    ),
]


async def list_repos(arguments: dict) -> str:
    org_filter = arguments.get("org")
    lang_filter = arguments.get("language")
    results = MOCK_REPOS
    if org_filter:
        results = [r for r in results if r["org"].lower() == org_filter.lower()]
    if lang_filter:
        results = [r for r in results if r["language"].lower() == lang_filter.lower()]
    return json.dumps(results)


async def on_list_tools(request, params):
    return ListToolsResult(tools=TOOLS)


async def on_call_tool(request, params: CallToolRequestParams):
    arguments = params.arguments or {}
    if params.name == "list_repos":
        text = await list_repos(arguments)
    else:
        text = f"Unknown tool: {params.name}"
    return CallToolResult(content=[TextContent(type="text", text=text)])


server = Server(
    name="fake-github",
    version="0.1.0",
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
