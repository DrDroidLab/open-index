"""droid-brain command line.

    droid-brain init <name> [dir]      scaffold a new brain
    droid-brain add-doc-type <name>    add a doc_type schema stub
    droid-brain add-entity <file>      validate + store an entity JSON file
    droid-brain index                  (re)load entities/ into the search index
    droid-brain ingest <connector>     run a connector to pull entities from MCP
    droid-brain search <query>         search from the terminal
    droid-brain ui                     launch the Streamlit map explorer
    droid-brain mcp                    run the MCP server (stdio)

`--brain <dir>` selects the brain directory (default: current directory).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    add_completion=False,
    help="Build and query a domain-agnostic context graph about your company.",
)

BrainOpt = typer.Option(".", "--brain", "-b", help="Brain directory.")


def _open_brain(brain_dir: str):
    from droid_brain.brain import Brain

    try:
        return Brain.open(brain_dir)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def init(
    name: str = typer.Argument(..., help="Name of the brain."),
    directory: str = typer.Argument(None, help="Target directory (default: ./<name>)."),
):
    """Scaffold a new brain directory."""
    from droid_brain.scaffold import init_brain

    target = Path(directory) if directory else Path(name)
    if (target / "brain.yaml").exists():
        typer.secho(f"{target}/brain.yaml already exists", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    init_brain(target, name)
    typer.secho(f"✓ created brain '{name}' in {target}/", fg=typer.colors.GREEN)
    typer.echo(f"  next: droid-brain index --brain {target} && droid-brain ui --brain {target}")


@app.command("add-doc-type")
def add_doc_type(
    name: str = typer.Argument(..., help="doc_type name, e.g. 'issue'."),
    brain: str = BrainOpt,
    color: Optional[str] = typer.Option(None, help="Hex node color."),
    storage: str = typer.Option("index", help="Source of truth: 'index' (DB) or 'file' (git)."),
):
    """Add a doc_type schema stub under doc_types/."""
    from droid_brain.scaffold import DOC_TYPE_TEMPLATE, color_for_index

    if storage not in ("index", "file"):
        typer.secho("--storage must be 'index' or 'file'", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    root = Path(brain)
    dt_dir = root / "doc_types"
    dt_dir.mkdir(parents=True, exist_ok=True)
    target = dt_dir / f"{name}.yaml"
    if target.exists():
        typer.secho(f"{target} already exists", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    chosen = color or color_for_index(len(list(dt_dir.glob("*.yaml"))))
    content = DOC_TYPE_TEMPLATE.format(name=name, color=chosen)
    if storage != "index":
        content = content.replace("storage: index", f"storage: {storage}")
    target.write_text(content)
    typer.secho(f"✓ added doc_type '{name}' (storage: {storage}) → {target}",
                fg=typer.colors.GREEN)


@app.command("add-entity")
def add_entity(
    file: str = typer.Argument(..., help="Path to an entity JSON file."),
    brain: str = BrainOpt,
):
    """Validate an entity file and store it in the brain."""
    from droid_brain.models import Entity

    b = _open_brain(brain)
    raw = json.loads(Path(file).read_text())
    records = raw if isinstance(raw, list) else [raw]
    added = 0
    for rec in records:
        entity = Entity.from_dict(rec)
        errors = b.validate_entity(entity)
        if errors:
            typer.secho(f"✗ {entity.id}: {'; '.join(errors)}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        b.put_entity(entity)  # honors the doc_type's storage policy
        added += 1
    typer.secho(f"✓ stored {added} entity(ies)", fg=typer.colors.GREEN)


@app.command()
def index(brain: str = BrainOpt):
    """(Re)load all entities/**/*.json into the search index."""
    b = _open_brain(brain)
    count = b.index()
    typer.secho(f"✓ indexed {count} entities across {len(b.config.doc_types)} doc_types",
                fg=typer.colors.GREEN)


@app.command()
def ingest(
    connector: str = typer.Argument(..., help="Connector name (see connectors/)."),
    brain: str = BrainOpt,
):
    """Run a connector to pull entities from an MCP server into the brain."""
    from droid_brain.connectors.runner import ingest as run_ingest

    b = _open_brain(brain)
    try:
        result = run_ingest(b, connector)
    except KeyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho(f"✓ {result.connector}: created/updated {result.created} entities",
                fg=typer.colors.GREEN)
    for err in result.errors:
        typer.secho(f"  ! {err}", fg=typer.colors.YELLOW, err=True)


@app.command()
def search(
    query: str = typer.Argument("", help="Search text (empty = list everything)."),
    brain: str = BrainOpt,
    doc_type: Optional[list[str]] = typer.Option(None, "--doc-type", "-t"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """Search the brain from the terminal."""
    b = _open_brain(brain)
    results = b.search(query=query or None, doc_types=doc_type, limit=limit)
    typer.echo(f"{results.total} match(es)  {dict(results.doc_type_counts)}")
    for r in results.results:
        typer.echo(f"  [{r['doc_type']}] {r['id']}  ({r['score']:.2f})  {r['name']}")


@app.command()
def run(
    brain: str = BrainOpt,
    force: bool = typer.Option(False, "--force", help="Run all connectors regardless of schedule."),
    loop: Optional[int] = typer.Option(None, "--loop", help="Repeat every N seconds (foreground daemon)."),
):
    """Run every connector whose schedule is due. Wire into cron/CI, or --loop."""
    import time

    from droid_brain.connectors.runner import run_due

    b = _open_brain(brain)

    def _once():
        results = run_due(b, force=force)
        ran = [r for r in results if not r.skipped]
        for r in ran:
            typer.secho(f"✓ {r.connector}: {r.created} entities"
                        + (f" ({len(r.errors)} errors)" if r.errors else ""),
                        fg=typer.colors.GREEN)
        skipped = [r.connector for r in results if r.skipped]
        if skipped:
            typer.echo(f"  not due: {', '.join(skipped)}")
        if not results:
            typer.echo("  (no connectors)")

    _once()
    if loop:
        while True:
            time.sleep(loop)
            _once()


@app.command()
def validate(brain: str = BrainOpt):
    """Validate brain.yaml, all doc_type schemas, and every entity file."""
    import json

    from droid_brain.models import Entity

    b = _open_brain(brain)
    problems: list[str] = []
    validated = 0
    from droid_brain.config import iter_entity_files

    for path in iter_entity_files(b.config.root):
        try:
            raw = json.loads(Path(path).read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}: invalid JSON ({exc})")
            continue
        for rec in (raw if isinstance(raw, list) else [raw]):
            try:
                entity = Entity.from_dict(rec)
            except Exception as exc:
                problems.append(f"{path.name}: {exc}")
                continue
            errors = b.validate_entity(entity)
            if errors:
                problems.extend(f"{entity.id}: {e}" for e in errors)
            else:
                validated += 1

    if problems:
        for p in problems:
            typer.secho(f"✗ {p}", fg=typer.colors.RED, err=True)
        typer.secho(f"{len(problems)} problem(s)", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho(
        f"✓ valid — {len(b.config.doc_types)} doc_types, {validated} entities",
        fg=typer.colors.GREEN,
    )


@app.command("list-connectors")
def list_connectors(brain: str = BrainOpt):
    """List connectors available in this brain."""
    from droid_brain.connectors.runner import discover_connectors

    b = _open_brain(brain)
    found = discover_connectors(b)
    if not found:
        typer.echo("(no connectors in connectors/)")
        return
    for name, cls in sorted(found.items()):
        where = f" → {cls.mcp_url}" if cls.mcp_url else ""
        typer.echo(f"  {name}{where}")


@app.command()
def ui(
    brain: str = BrainOpt,
    port: int = typer.Option(8501, help="Streamlit port."),
):
    """Launch the Streamlit map explorer."""
    import os
    import subprocess

    app_path = Path(__file__).parent / "ui" / "app.py"
    env = dict(os.environ, DROID_BRAIN_DIR=str(Path(brain).resolve()))
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.port", str(port),
    ]
    try:
        subprocess.run(cmd, env=env, check=True)
    except FileNotFoundError:
        typer.secho("Streamlit not installed: pip install 'droid-brain[ui]'",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def mcp(
    brain: str = BrainOpt,
    read_only: bool = typer.Option(False, "--read-only", help="Expose only read tools."),
):
    """Run the MCP server over stdio (local agents / Claude Code)."""
    from droid_brain.mcp_server import serve

    serve(str(Path(brain).resolve()), read_only=read_only)


@app.command()
def serve(
    brain: str = BrainOpt,
    host: str = typer.Option("0.0.0.0", help="Bind host."),
    port: int = typer.Option(8080, help="Bind port."),
    token: Optional[str] = typer.Option(
        None, envvar="DROID_BRAIN_TOKEN",
        help="Bearer token required on requests (or set DROID_BRAIN_TOKEN).",
    ),
    read_only: bool = typer.Option(False, "--read-only", help="Expose only read tools."),
):
    """Serve the MCP server over HTTP so remote/cloud agents connect by URL.

    Register http://<host>:<port>/mcp as a remote MCP server in your agent.
    Pair with the OpenSearch backend for a shared, multi-writer brain.
    """
    from droid_brain.mcp_server import serve_http

    mode = "read-only" if read_only else "read+write"
    typer.secho(f"serving brain '{Path(brain).name}' MCP ({mode}) at http://{host}:{port}/mcp",
                fg=typer.colors.GREEN)
    serve_http(str(Path(brain).resolve()), host=host, port=port, token=token, read_only=read_only)


if __name__ == "__main__":
    app()
