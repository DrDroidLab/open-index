"""CLI for Droid Brain — create and query brains from the terminal.

Usage:
  droid-brain create-brain <name>
  droid-brain create-doctype <brain> <name>
  droid-brain create-entity <brain> <doctype>
  droid-brain search <brain> <query>
  droid-brain list-brains
  droid-brain structure <brain>
  droid-brain mcp-server [--transport stdio|sse]
  droid-brain seed-demo [brain_name]
  droid-brain connector add <name> --mcp-cmd ... --tool ... --brain ... --doctype ... --field-mapping '...'
  droid-brain connector list | run | runs <name>
  droid-brain cron
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import click

from droid_brain.core import DroidBrain

@click.group()
@click.option(
    "--remote-url",
    default=None,
    envvar="DROID_BRAIN_REMOTE_URL",
    help="Remote MCP server URL (e.g. http://host:8000). When set, CLI talks to remote brain.",
)
@click.pass_context
def cli(ctx: click.Context, remote_url: Optional[str]) -> None:
    """Droid Brain — structured organisational knowledge for AI agents."""
    ctx.ensure_object(dict)

    if remote_url:
        from droid_brain.remote import RemoteDroidBrain

        ctx.obj["db"] = RemoteDroidBrain(remote_url)
        ctx.obj["remote"] = True
    else:
        ctx.obj["db"] = DroidBrain()
        ctx.obj["remote"] = False


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _require_local(ctx: click.Context, command: str) -> None:
    """Exit with a clear message if the CLI is in remote mode."""
    if ctx.obj.get("remote"):
        click.echo(
            f"❌ '{command}' is not available in remote mode.\n"
            f"   Remote brains are read-only via MCP. Run locally to create or modify data.",
            err=True,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Brain management
# ---------------------------------------------------------------------------


@cli.command("create-brain")
@click.argument("name")
@click.option("--description", "-d", default="", help="Brain description")
@click.pass_context
def create_brain(ctx: click.Context, name: str, description: str) -> None:
    """Create a new brain."""
    _require_local(ctx, "create-brain")
    db: DroidBrain = ctx.obj["db"]
    brain = db.create_brain(name, description)
    click.echo(f"✅ Brain '{name}' created.")
    click.echo(json.dumps(brain, indent=2))


@cli.command("list-brains")
@click.pass_context
def list_brains(ctx: click.Context) -> None:
    """List all brains."""
    db: DroidBrain = ctx.obj["db"]
    brains = db.list_brains()
    if not brains:
        click.echo("No brains found. Create one with: droid-brain create-brain <name>")
        return
    for b in brains:
        click.echo(f"  🧠 {b['name']}" + (f" — {b.get('description', '')}" if b.get("description") else ""))


@cli.command("delete-brain")
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to delete this brain and all its data?")
@click.pass_context
def delete_brain(ctx: click.Context, name: str) -> None:
    """Delete a brain and all its entities."""
    _require_local(ctx, "delete-brain")
    db: DroidBrain = ctx.obj["db"]
    db.delete_brain(name)
    click.echo(f"🗑️  Brain '{name}' deleted.")


# ---------------------------------------------------------------------------
# DocType management
# ---------------------------------------------------------------------------


@cli.command("create-doctype")
@click.argument("brain")
@click.argument("name")
@click.option("--description", "-d", default="", help="DocType description")
@click.option(
    "--fields",
    "-f",
    default="[]",
    help="JSON array of field definitions, e.g. '[{\"name\":\"title\",\"field_type\":\"string\"}]'",
)
@click.pass_context
def create_doctype(
    ctx: click.Context, brain: str, name: str, description: str, fields: str
) -> None:
    """Create a new doc_type within a brain."""
    _require_local(ctx, "create-doctype")
    db: DroidBrain = ctx.obj["db"]
    try:
        fields_data = json.loads(fields)
    except json.JSONDecodeError:
        click.echo("❌ Invalid JSON for --fields. Must be a JSON array.", err=True)
        sys.exit(1)
    dt = db.create_doctype(brain, name, description, fields_data)
    click.echo(f"✅ DocType '{name}' created in brain '{brain}'.")
    click.echo(json.dumps(dt, indent=2))


@cli.command("list-doctypes")
@click.argument("brain")
@click.pass_context
def list_doctypes(ctx: click.Context, brain: str) -> None:
    """List all doc_types in a brain."""
    db: DroidBrain = ctx.obj["db"]
    doctypes = db.list_doctypes(brain)
    if not doctypes:
        click.echo(f"No doc_types in brain '{brain}'.")
        return
    for dt in doctypes:
        fields = [f["name"] for f in dt.get("schema_fields", [])]
        click.echo(f"  📄 {dt['name']} — fields: {', '.join(fields) if fields else 'none'}")


# ---------------------------------------------------------------------------
# Entity management
# ---------------------------------------------------------------------------


@cli.command("create-entity")
@click.argument("brain")
@click.argument("doctype")
@click.option(
    "--data",
    "-d",
    default="{}",
    help="JSON object with entity data, e.g. '{\"title\":\"My Service\"}'",
)
@click.option("--file", "-f", type=click.Path(exists=True), help="JSON file with entity data")
@click.pass_context
def create_entity(
    ctx: click.Context,
    brain: str,
    doctype: str,
    data: str,
    file: Optional[str],
) -> None:
    """Create a new entity in a brain."""
    _require_local(ctx, "create-entity")
    db: DroidBrain = ctx.obj["db"]
    if file:
        with open(file) as fh:
            data_dict = json.load(fh)
    else:
        try:
            data_dict = json.loads(data)
        except json.JSONDecodeError:
            click.echo("❌ Invalid JSON for --data.", err=True)
            sys.exit(1)
    entity = db.create_entity(brain, doctype, data_dict)
    click.echo(f"✅ Entity '{entity['entity_id']}' created in brain '{brain}'.")
    click.echo(json.dumps(entity, indent=2))


@cli.command("get-entity")
@click.argument("brain")
@click.argument("entity_id")
@click.pass_context
def get_entity(ctx: click.Context, brain: str, entity_id: str) -> None:
    """Fetch a specific entity by ID."""
    db: DroidBrain = ctx.obj["db"]
    entity = db.get_entity(brain, entity_id)
    if not entity:
        click.echo(f"❌ Entity '{entity_id}' not found.", err=True)
        sys.exit(1)
    click.echo(json.dumps(entity, indent=2))


@cli.command("list-entities")
@click.argument("brain")
@click.option("--doctype", "-t", default=None, help="Filter by doc_type")
@click.option("--size", "-n", default=50, help="Max results")
@click.pass_context
def list_entities(
    ctx: click.Context, brain: str, doctype: Optional[str], size: int
) -> None:
    """List entities in a brain."""
    db: DroidBrain = ctx.obj["db"]
    entities = db.list_entities(brain, doc_type=doctype, size=size)
    if not entities:
        click.echo("No entities found.")
        return
    for e in entities:
        click.echo(
            f"  🔹 {e['entity_id']} [{e['doc_type']}] — {json.dumps(e.get('data', {}))}"
        )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@cli.command("search")
@click.argument("brain")
@click.argument("query")
@click.option("--doctype", "-t", default=None, help="Filter by doc_type")
@click.option("--size", "-n", default=20, help="Max results")
@click.pass_context
def search_brain(
    ctx: click.Context,
    brain: str,
    query: str,
    doctype: Optional[str],
    size: int,
) -> None:
    """Search entities in a brain."""
    db: DroidBrain = ctx.obj["db"]
    results = db.search(brain, query_text=query, doc_type=doctype, size=size)
    if not results:
        click.echo(f"No results for '{query}'.")
        return
    click.echo(f"Found {len(results)} result(s):")
    for r in results:
        click.echo(f"\n  🔹 {r['entity_id']} [{r.get('doc_type', '?')}]")
        click.echo(f"     {json.dumps(r.get('data', {}))}")


# ---------------------------------------------------------------------------
# Brain structure
# ---------------------------------------------------------------------------


@cli.command("structure")
@click.argument("brain")
@click.pass_context
def structure(ctx: click.Context, brain: str) -> None:
    """Show brain structure — doc_types, counts, examples."""
    db: DroidBrain = ctx.obj["db"]
    bs = db.get_brain_structure(brain)
    click.echo(f"Brain: {bs.brain_name}")
    click.echo(f"Total entities: {bs.total_entities}")
    click.echo(f"Doc types: {len(bs.doc_types)}")
    click.echo("-" * 40)
    for dt in bs.doc_types:
        click.echo(f"\n📁 {dt['name']} ({dt['entity_count']} entities)")
        if dt.get("description"):
            click.echo(f"   {dt['description']}")
        if dt.get("examples"):
            click.echo("   Examples:")
            for ex in dt["examples"]:
                click.echo(f"     {json.dumps(ex)}")


# ---------------------------------------------------------------------------
# MCP Server launcher
# ---------------------------------------------------------------------------


@cli.command("mcp-server")
@click.option("--transport", default="stdio", type=click.Choice(["stdio", "sse"]))
@click.option("--port", default=8000, help="Port for SSE transport")
@click.pass_context
def mcp_server(ctx: click.Context, transport: str, port: int) -> None:
    """Start the MCP server."""
    from droid_brain.mcp_server import main as mcp_main

    sys.argv = [
        "mcp-server",
        "--transport",
        transport,
        "--port",
        str(port),
    ]
    mcp_main()


# ---------------------------------------------------------------------------
# Shared seed helper — callable from both CLI and Streamlit
# ---------------------------------------------------------------------------


def seed_demo_data(db: DroidBrain, brain_name: str = "demo") -> None:
    """Seed a brain with demo infrastructure data (services, dashboards, runbooks).

    Can be called directly with any DroidBrain instance — no Click dependency.
    """
    db.create_brain(brain_name, "Demo infrastructure brain")

    db.create_doctype(
        brain_name,
        "service",
        "A microservice or application",
        [
            {"name": "name", "field_type": "string", "required": True, "search_type": "syntactic"},
            {"name": "team", "field_type": "string", "required": True, "search_type": "syntactic"},
            {"name": "repo_url", "field_type": "string", "search_type": "syntactic"},
            {"name": "tier", "field_type": "string", "search_type": "syntactic"},
            {"name": "description", "field_type": "string", "processing_type": "text", "search_type": "semantic"},
            {"name": "metadata", "field_type": "object", "fields": [
                {"name": "deployment", "field_type": "string", "search_type": "syntactic"},
                {"name": "owner_email", "field_type": "string", "search_type": "syntactic"},
                {"name": "slack_channel", "field_type": "string", "search_type": "syntactic"},
            ]},
        ],
    )

    db.create_doctype(
        brain_name,
        "dashboard",
        "A monitoring dashboard",
        [
            {"name": "title", "field_type": "string", "required": True, "search_type": "syntactic"},
            {"name": "url", "field_type": "string", "search_type": "syntactic"},
            {"name": "service", "field_type": "string", "search_type": "syntactic"},
            {"name": "description", "field_type": "string", "processing_type": "text", "search_type": "semantic"},
        ],
    )

    db.create_doctype(
        brain_name,
        "runbook",
        "An operational runbook",
        [
            {"name": "title", "field_type": "string", "required": True, "search_type": "syntactic"},
            {"name": "service", "field_type": "string", "search_type": "syntactic"},
            {"name": "severity", "field_type": "string", "search_type": "syntactic"},
            {"name": "steps", "field_type": "string", "processing_type": "text", "search_type": "semantic"},
        ],
    )

    services = [
        {"name": "api-gateway", "team": "platform", "repo_url": "https://github.com/acme/api-gateway", "tier": "tier-0", "description": "Main API gateway handling all external traffic. Uses NGINX + Lua for routing and authentication.", "metadata": {"deployment": "kubernetes", "owner_email": "platform@acme.com", "slack_channel": "#platform-alerts"}},
        {"name": "user-service", "team": "backend", "repo_url": "https://github.com/acme/user-service", "tier": "tier-1", "description": "User management service — registration, auth, and profiles. Backed by PostgreSQL.", "metadata": {"deployment": "kubernetes", "owner_email": "backend@acme.com", "slack_channel": "#backend-alerts"}},
        {"name": "payment-worker", "team": "payments", "repo_url": "https://github.com/acme/payment-worker", "tier": "tier-1", "description": "Async payment processing worker consuming from Kafka. Integrates with Stripe.", "metadata": {"deployment": "kubernetes", "owner_email": "payments@acme.com", "slack_channel": "#payments-alerts"}},
        {"name": "notification-service", "team": "backend", "repo_url": "https://github.com/acme/notification-service", "tier": "tier-2", "description": "Sends email, SMS, and push notifications via AWS SNS + SendGrid.", "metadata": {"deployment": "kubernetes", "owner_email": "backend@acme.com", "slack_channel": "#backend-alerts"}},
        {"name": "inventory-db", "team": "data-platform", "repo_url": "https://github.com/acme/inventory-db", "tier": "tier-0", "description": "Core inventory database cluster — PostgreSQL with read replicas.", "metadata": {"deployment": "kubernetes", "owner_email": "data@acme.com", "slack_channel": "#data-alerts"}},
    ]
    for s in services:
        db.create_entity(brain_name, "service", s)

    dashboards = [
        {"title": "API Gateway Overview", "url": "https://grafana.acme.com/d/api-gateway", "service": "api-gateway", "description": "Request rate, latency percentiles, error rate, and upstream health for the API gateway."},
        {"title": "User Service Health", "url": "https://grafana.acme.com/d/user-service", "service": "user-service", "description": "Registration rate, login success rate, DB query latency, and cache hit ratio."},
        {"title": "Payment Processing", "url": "https://grafana.acme.com/d/payments", "service": "payment-worker", "description": "Kafka consumer lag, Stripe API latency, success/failure rate per payment method."},
    ]
    for d in dashboards:
        db.create_entity(brain_name, "dashboard", d)

    runbooks = [
        {"title": "API Gateway 5xx Spike", "service": "api-gateway", "severity": "critical", "steps": "1. Check NGINX error logs in CloudWatch. 2. Verify upstream service health. 3. Check for recent deployments. 4. Roll back if correlated. 5. Scale up gateway instances if under load."},
        {"title": "Payment Processing Delay", "service": "payment-worker", "severity": "high", "steps": "1. Check Kafka consumer lag in Burrow. 2. Verify Stripe API status page. 3. Check worker pod resource usage. 4. Restart stuck partitions if needed."},
        {"title": "Inventory DB Replication Lag", "service": "inventory-db", "severity": "critical", "steps": "1. Check replication slot status. 2. Verify network between primary and replicas. 3. Check for long-running queries on primary. 4. Failover to replica if primary is degraded."},
    ]
    for r in runbooks:
        db.create_entity(brain_name, "runbook", r)


# ---------------------------------------------------------------------------
# Seed CLI command (thin wrapper around the shared helper)
# ---------------------------------------------------------------------------


@cli.command("seed-demo")
@click.argument("brain_name", default="demo")
@click.pass_context
def seed_demo(ctx: click.Context, brain_name: str) -> None:
    """Seed a brain with demo data (infrastructure example)."""
    _require_local(ctx, "seed-demo")
    db: DroidBrain = ctx.obj["db"]
    seed_demo_data(db, brain_name)

    structure = db.get_brain_structure(brain_name)
    click.echo(f"✅ Demo brain '{brain_name}' seeded!")
    click.echo(f"   Doc types: {len(structure.doc_types)}")
    for dt in structure.doc_types:
        click.echo(f"     • {dt['name']}: {dt['entity_count']} entities")
    click.echo(f"   Total entities: {structure.total_entities}")
    click.echo("\nTry:")
    click.echo(f"  droid-brain structure {brain_name}")
    click.echo(f"  droid-brain search {brain_name} gateway")
    click.echo(f"  streamlit run app.py  (then select '{brain_name}' in the UI)")


@cli.command("start")
@click.argument("brain_name", default="demo")
@click.option("--port", default=8501, help="Streamlit port")
@click.pass_context
def start(ctx: click.Context, brain_name: str, port: int) -> None:
    """Launch the Droid Brain UI. Seeds demo data if brain doesn't exist yet."""
    _require_local(ctx, "start")
    db: DroidBrain = ctx.obj["db"]
    brains = {b["name"] for b in db.list_brains()}

    if brain_name in brains:
        s = db.get_brain_structure(brain_name)
        click.echo(f"🧠 Using existing brain '{brain_name}' ({s.total_entities} entities).")
    else:
        seed_demo_data(db, brain_name)
        s = db.get_brain_structure(brain_name)
        click.echo(f"🌱 Seeded demo brain '{brain_name}' ({s.total_entities} entities).")

    click.echo(f"🚀 Launching UI at http://localhost:{port} ...")

    import subprocess
    from pathlib import Path

    app_path = Path(__file__).parent.parent / "app.py"

    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(app_path),
         "--server.address", "0.0.0.0", "--server.port", str(port)],
    )


# ---------------------------------------------------------------------------
# Connector & cron commands
# ---------------------------------------------------------------------------


@cli.group("connector")
def connector_group() -> None:
    """Manage connectors — MCP → brain extraction pipelines."""


@connector_group.command("add")
@click.argument("name")
@click.option("--mcp-cmd", default=None, help="MCP server command, e.g. 'python examples/github_mcp.py'")
@click.option("--tool", required=True, help="MCP tool name to call")
@click.option("--brain", required=True, help="Target brain name")
@click.option("--doctype", required=True, help="Target doc_type")
@click.option("--field-mapping", required=True, help="JSON mapping: tool_field→entity_field")
@click.option("--cron", default="", help="Cron expression or 'every_6h', 'every_30m'")
@click.option("--transform", default=None, help="Python expression to transform each item")
@click.option("--handler", "handler_path", default=None,
              help="Python import path for in-process handler, e.g. 'examples.github_mcp:list_repos'. Fast path, no subprocess.")
@click.pass_context
def connector_add(
    ctx: click.Context,
    name: str,
    mcp_cmd: Optional[str],
    tool: str,
    brain: str,
    doctype: str,
    field_mapping: str,
    cron: str,
    transform: Optional[str],
    handler_path: Optional[str],
) -> None:
    """Register a new connector (scheduled extraction pipeline)."""
    from droid_brain.cron import CronManager

    if not mcp_cmd and not handler_path:
        click.echo("❌ Either --mcp-cmd or --handler is required.", err=True)
        sys.exit(1)

    try:
        mapping = json.loads(field_mapping)
    except json.JSONDecodeError:
        click.echo("❌ --field-mapping must be valid JSON.", err=True)
        sys.exit(1)

    mgr = CronManager()
    result = mgr.add(
        name=name,
        mcp_command=mcp_cmd or "",
        tool_name=tool,
        brain_name=brain,
        doc_type=doctype,
        field_mapping=mapping,
        cron_expr=cron,
        transform=transform,
        handler_path=handler_path,
    )
    mode = " (in-process)" if handler_path else " (subprocess)"
    click.echo(f"✅ Connector '{result['name']}' registered{mode}."
               + (f" Schedule: {cron}" if cron else " (manual only)"))


@connector_group.command("list")
def connector_list() -> None:
    """List registered connectors."""
    from droid_brain.cron import CronManager

    mgr = CronManager()
    connectors = mgr.list()
    if not connectors:
        click.echo("No connectors registered.")
        click.echo("Add one: droid-brain connector add <name> --mcp-cmd ... --tool ... --brain ... --doctype ... --field-mapping '...'")
        return
    for c in connectors:
        status = "✅" if c["enabled"] else "⏸️"
        cron = c["cron_expr"] or "(manual)"
        last = c["last_run"][:19] if c["last_run"] else "never"
        click.echo(f"  {status} {c['name']} — schedule: {cron} — last run: {last}")


@connector_group.command("run")
@click.argument("name")
def connector_run(name: str) -> None:
    """Run a connector immediately."""
    from droid_brain.cron import CronManager

    mgr = CronManager()
    click.echo(f"▶️  Running connector '{name}'...")
    try:
        result = asyncio.run(mgr.run_connector(name))
        click.echo(f"✅ {result['entities_created']} entities created in '{result['brain']}'.")
    except Exception as exc:
        click.echo(f"❌ Failed: {exc}", err=True)
        sys.exit(1)


@connector_group.command("remove")
@click.argument("name")
def connector_remove(name: str) -> None:
    """Remove a registered connector."""
    from droid_brain.cron import CronManager

    mgr = CronManager()
    if mgr.remove(name):
        click.echo(f"🗑️  Connector '{name}' removed.")
    else:
        click.echo(f"❌ Connector '{name}' not found.", err=True)


@connector_group.command("runs")
@click.argument("name")
@click.option("--limit", default=10, help="Number of runs to show")
def connector_runs(name: str, limit: int) -> None:
    """Show recent runs for a connector."""
    from droid_brain.cron import CronManager

    mgr = CronManager()
    runs = mgr.get_runs(name, limit=limit)
    if not runs:
        click.echo(f"No runs recorded for '{name}'.")
        return
    for r in runs:
        status = "✅" if r["status"] == "success" else "❌"
        click.echo(
            f"  {status} {r['started_at'][:19]} — {r['entities_created']} entities"
            + (f" — {r['error'][:60]}" if r.get("error") else "")
        )


@cli.command("cron")
def cron_scheduler() -> None:
    """Start the Droid Brain cron scheduler (runs until Ctrl+C)."""
    from droid_brain.cron import CronManager

    mgr = CronManager()
    try:
        asyncio.run(mgr.start_scheduler())
    except KeyboardInterrupt:
        click.echo("\n👋 Scheduler stopped.")


if __name__ == "__main__":
    cli()
