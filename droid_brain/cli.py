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
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import click

from droid_brain.core import DroidBrain

@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Droid Brain — structured organisational knowledge for AI agents."""
    ctx.ensure_object(dict)
    ctx.obj["db"] = DroidBrain()


# ---------------------------------------------------------------------------
# Brain management
# ---------------------------------------------------------------------------


@cli.command("create-brain")
@click.argument("name")
@click.option("--description", "-d", default="", help="Brain description")
@click.pass_context
def create_brain(ctx: click.Context, name: str, description: str) -> None:
    """Create a new brain."""
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
        {"name": "api-gateway", "team": "platform", "repo_url": "https://github.com/acme/api-gateway", "tier": "tier-0", "description": "Main API gateway handling all external traffic. Uses NGINX + Lua for routing and authentication. Handles approximately 50k requests per second at peak."},
        {"name": "user-service", "team": "backend", "repo_url": "https://github.com/acme/user-service", "tier": "tier-1", "description": "User management service — handles registration, authentication, and profile management. Backed by PostgreSQL."},
        {"name": "payment-worker", "team": "payments", "repo_url": "https://github.com/acme/payment-worker", "tier": "tier-1", "description": "Async payment processing worker consuming from Kafka. Integrates with Stripe and internal ledger."},
        {"name": "notification-service", "team": "backend", "repo_url": "https://github.com/acme/notification-service", "tier": "tier-2", "description": "Sends email, SMS, and push notifications. Uses AWS SNS + SendGrid."},
        {"name": "inventory-db", "team": "data-platform", "repo_url": "https://github.com/acme/inventory-db", "tier": "tier-0", "description": "Core inventory database cluster — PostgreSQL with read replicas. Critical path for all order flows."},
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
    """Seed demo brain and launch Streamlit — single command to get started."""
    db: DroidBrain = ctx.obj["db"]
    seed_demo_data(db, brain_name)

    structure = db.get_brain_structure(brain_name)
    click.echo(f"🌱 Demo brain '{brain_name}' seeded ({structure.total_entities} entities).")
    click.echo(f"🚀 Launching UI at http://localhost:{port} ...")

    import subprocess

    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", "app.py",
         "--server.address", "0.0.0.0", "--server.port", str(port)],
    )


if __name__ == "__main__":
    cli()
