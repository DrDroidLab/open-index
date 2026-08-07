"""Example connector: pull "issues" from an MCP server into the brain.

This is the codified-script pattern — loops, data manipulation, hitting an MCP
server's tools — distilled to the smallest useful shape. Point `mcp_url` at any
MCP server that exposes a tool returning issue-like records, set the tool name
and field mapping, and run:

    open-index ingest example-issues --brain examples/support-brain

Because most people won't have a live MCP server handy, `extract_issues_demo`
also shows the pattern working against in-memory sample data (no network), so
`ingest` produces entities out of the box. Delete it once you wire a real server.
"""

from __future__ import annotations

from open_index.connectors import Connector, EntitySpec


class ExampleIssuesConnector(Connector):
    name = "example-issues"

    # Point this at a real MCP server to use extract_issues (below).
    mcp_url = None  # e.g. "https://my-mcp-server.example.com/mcp"
    mcp_auth_headers = None  # e.g. {"Authorization": "Bearer ${MY_TOKEN}"}
    schedule = "daily"  # `open-index run` triggers it when due; "manual" to disable

    #: Which MCP tool returns issues, and how its fields map onto our schema.
    tool_name = "list_issues"
    tool_arguments: dict = {}
    target_doc_type = "issue"

    # ---- real MCP extraction (enabled when mcp_url is set) ----------------- #
    def extract_issues(self):
        if self.mcp is None:
            return  # no server configured; the demo extractor runs instead
        # paginate() loops over cursor pages and flattens tool content to records.
        for item in self.paginate(self.tool_name, self.tool_arguments, result_key="issues"):
            yield self._to_spec(item)

    # ---- offline demo so `ingest` works without a server ------------------ #
    def extract_issues_demo(self):
        if self.mcp is not None:
            return  # a real server is configured; skip the demo
        sample = [
            {
                "id": "cart-abandonment",
                "title": "High cart abandonment after promo",
                "severity": "medium",
                "state": "open",
                "product": "checkout",
            },
            {
                "id": "wallet-not-supported",
                "title": "Wallet payment method not supported in EU",
                "severity": "low",
                "state": "open",
                "product": "checkout",
            },
        ]
        for item in sample:
            yield self._to_spec(item)

    # ---- shared mapping: source record -> typed entity + edges ------------ #
    def _to_spec(self, item: dict) -> EntitySpec:
        related = []
        product = item.get("product")
        if product:
            related.append((f"product:{product}", "affects product"))
        return EntitySpec(
            doc_type=self.target_doc_type,
            id=f"{self.target_doc_type}:{item['id']}",
            name=item.get("title", item["id"]),
            fields={
                "description": item.get("title", ""),
                "severity": item.get("severity", "unknown"),
                "status": item.get("state", "open"),
            },
            related_to=related,
        )
