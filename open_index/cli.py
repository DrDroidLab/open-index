"""open-index command line.

    open-index init <name> [dir]      scaffold a new brain
    open-index add-doc-type <name>    add a doc_type schema stub
    open-index add-entity <file>      validate + store an entity JSON file
    open-index index                  (re)load entities/ into the search index
    open-index ingest <connector>     run a connector to pull entities from MCP
    open-index search <query>         search from the terminal
    open-index ui                     launch the Streamlit map explorer
    open-index mcp                    run the MCP server (stdio)

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
    from open_index.brain import Brain

    try:
        return Brain.open(brain_dir)
    except (FileNotFoundError, ValueError) as exc:
        # ValueError covers a bad brain.yaml / env override (e.g. an unknown
        # search backend) — a user error, not something to show a traceback for.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def init(
    name: str = typer.Argument(..., help="Name of the brain."),
    directory: str = typer.Argument(None, help="Target directory (default: ./<name>)."),
):
    """Scaffold a new brain directory."""
    from open_index.scaffold import init_brain

    target = Path(directory) if directory else Path(name)
    if (target / "brain.yaml").exists():
        typer.secho(f"{target}/brain.yaml already exists", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    init_brain(target, name)
    typer.secho(f"✓ created brain '{name}' in {target}/", fg=typer.colors.GREEN)
    typer.echo(f"  next: open-index index --brain {target} && open-index ui --brain {target}")


@app.command("add-doc-type")
def add_doc_type(
    name: str = typer.Argument(..., help="doc_type name, e.g. 'issue'."),
    brain: str = BrainOpt,
    color: Optional[str] = typer.Option(None, help="Hex node color."),
    storage: str = typer.Option("index", help="Source of truth: 'index' (DB) or 'file' (git)."),
):
    """Add a doc_type schema stub under doc_types/."""
    from open_index.scaffold import DOC_TYPE_TEMPLATE, color_for_index

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
    from open_index.models import Entity

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


@app.command("import")
def import_entities(
    file: str = typer.Argument(..., help="A .json (array), .jsonl, or .csv file."),
    brain: str = BrainOpt,
    doc_type: Optional[str] = typer.Option(
        None, "--doc-type", "-t",
        help="doc_type for rows that don't name one (required for CSV).",
    ),
    id_field: str = typer.Option(
        "id", "--id-field",
        help="Which column/key holds the id or slug. Slugs are prefixed with the doc_type.",
    ),
    asserted_by: Optional[str] = typer.Option(
        None, "--asserted-by",
        help="Attribution for the whole batch, e.g. 'import:crm-2026-08'.",
    ),
    confidence: Optional[float] = typer.Option(
        None, "--confidence", min=0.0, max=1.0,
        help="Confidence for the whole batch (0.0-1.0).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and report without writing."
    ),
):
    """Bulk-import entities from a JSON array, JSONL, or CSV file.

    Rows that fail validation are reported and skipped; the rest still land.

        open-index import people.csv --doc-type person --asserted-by import:hr
    """
    from open_index.bulk import load_entity_records

    b = _open_brain(brain)
    path = Path(file)
    if not path.exists():
        typer.secho(f"no such file: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    try:
        records, parse_errors = load_entity_records(
            path, doc_type=doc_type, id_field=id_field
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    from open_index.models import Entity, Provenance

    entities = []
    for i, rec in enumerate(records):
        try:
            entities.append(Entity.from_dict(rec))
        except (TypeError, ValueError, KeyError) as exc:
            parse_errors.append(f"row {i + 1}: {exc}")

    provenance = None
    if asserted_by or confidence is not None:
        from datetime import datetime, timezone

        provenance = Provenance(
            asserted_by=asserted_by,
            asserted_at=datetime.now(timezone.utc).isoformat(),
            confidence=confidence,
        )

    if dry_run:
        problems = list(parse_errors)
        valid = 0
        for entity in entities:
            errors = b.validate_entity(entity)
            problems.extend(f"{entity.id}: {e}" for e in errors)
            valid += not errors
        for problem in problems:
            typer.secho(f"  ✗ {problem}", fg=typer.colors.RED, err=True)
        typer.secho(
            f"dry run: {valid} entity(ies) would be written, "
            f"{len(problems)} problem(s) — nothing written",
            fg=typer.colors.YELLOW if problems else typer.colors.GREEN,
        )
        raise typer.Exit(1 if problems else 0)

    result = b.put_entities(entities, provenance=provenance)
    for err in parse_errors + result.errors:
        typer.secho(f"  ✗ {err}", fg=typer.colors.RED, err=True)

    failed = len(parse_errors) + result.failed
    typer.secho(
        f"✓ imported {result.written} entities"
        + (f" ({failed} skipped)" if failed else "")
        + (f", {len(result.paths)} file(s) written" if result.paths else ""),
        fg=typer.colors.YELLOW if failed else typer.colors.GREEN,
    )
    if failed:
        raise typer.Exit(1)


@app.command()
def index(
    brain: str = BrainOpt,
    reembed: bool = typer.Option(False, "--reembed", help="Recompute embeddings after indexing."),
):
    """(Re)load all entities/**/*.json into the search index."""
    b = _open_brain(brain)
    count = b.index()
    if reembed:
        b.reembed()
    typer.secho(
        f"✓ indexed {count} entities across {len(b.config.doc_types)} doc_types"
        + (" and recomputed embeddings" if reembed else ""),
        fg=typer.colors.GREEN,
    )
    # Surface per-entity failures rather than letting a partial load look complete.
    if b.index_errors:
        typer.secho(f"⚠ {len(b.index_errors)} entity file(s) skipped:", fg=typer.colors.YELLOW)
        for err in b.index_errors[:10]:
            typer.secho(f"    {err}", fg=typer.colors.YELLOW)
        if len(b.index_errors) > 10:
            typer.secho(f"    ... and {len(b.index_errors) - 10} more", fg=typer.colors.YELLOW)


@app.command()
def ingest(
    connector: str = typer.Argument(..., help="Connector name (see connectors/)."),
    brain: str = BrainOpt,
):
    """Run a connector to pull entities from an MCP server into the brain."""
    from open_index.connectors.runner import ingest as run_ingest

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
    results = b.search(query=query or None, doc_types=doc_type, limit=limit, source="cli")
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

    from open_index.connectors.runner import run_due

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

    from open_index.models import Entity

    b = _open_brain(brain)
    problems: list[str] = []
    validated = 0
    from open_index.config import iter_entity_files

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
    from open_index.connectors.runner import discover_connectors

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
    env = dict(os.environ, OPEN_INDEX_DIR=str(Path(brain).resolve()))
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.port", str(port),
    ]
    try:
        subprocess.run(cmd, env=env, check=True)
    except FileNotFoundError:
        typer.secho("Streamlit not installed: pip install 'open-index[ui]'",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def mcp(
    brain: str = BrainOpt,
    read_only: bool = typer.Option(
        False, "--read-only",
        help="Opt out of default read+write mode and expose only read tools.",
    ),
):
    """Run the MCP context layer over stdio (read+write by default)."""
    from open_index.mcp_server import serve

    serve(str(Path(brain).resolve()), read_only=read_only)


@app.command("mcp-config")
def mcp_config(
    brain: str = BrainOpt,
    url: Optional[str] = typer.Option(
        None, "--url",
        help="Remote brain URL (host:port or full https://…/mcp). Omit for a local stdio brain.",
    ),
    token: Optional[str] = typer.Option(
        None, "--token", envvar="OPEN_INDEX_TOKEN",
        help="Bearer token for a remote brain (or set OPEN_INDEX_TOKEN).",
    ),
    name: str = typer.Option("open-index", "--name", help="MCP server name in your agent."),
    cli: bool = typer.Option(False, "--cli", help="Print the `claude mcp add` one-liner instead of JSON."),
):
    """Print the MCP connection block to paste into your agent.

    Local brain (agent spawns the server itself):

        open-index mcp-config --brain ./my-brain

    Remote brain (an `open-index serve` reachable over the network):

        open-index mcp-config --url brain.internal:8080 --token $OPEN_INDEX_TOKEN

    Writes to stdout, so it pipes straight into a config file:

        open-index mcp-config > .mcp.json
    """
    from open_index.mcp_config import (
        as_claude_cli, as_json, local_stdio_config, remote_http_config,
    )

    if url:
        config = remote_http_config(url, token=token, server_name=name)
    else:
        root = Path(brain).expanduser().resolve()
        if not (root / "brain.yaml").exists():
            typer.secho(
                f"no brain.yaml in {root} — pass --brain <dir>, or --url for a remote brain",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(1)
        config = local_stdio_config(root, server_name=name)

    typer.echo(as_claude_cli(config, server_name=name) if cli else as_json(config))


@app.command()
def serve(
    brain: str = BrainOpt,
    host: str = typer.Option("0.0.0.0", help="Bind host."),
    port: int = typer.Option(8080, help="Bind port."),
    token: Optional[str] = typer.Option(
        None, envvar="OPEN_INDEX_TOKEN",
        help="Bearer token required on requests (or set OPEN_INDEX_TOKEN).",
    ),
    public_url: Optional[str] = typer.Option(
        None, "--public-url", envvar="OPEN_INDEX_PUBLIC_URL",
        help="Externally reachable URL, when behind a proxy/tunnel/load balancer. "
             "Only used to print correct connection details.",
    ),
    allowed_host: Optional[list[str]] = typer.Option(
        None, "--allowed-host", envvar="OPEN_INDEX_ALLOWED_HOSTS",
        help="Host header(s) to accept, when behind a proxy or load balancer. "
             "Repeatable. The host from --public-url is added automatically. "
             "Use '*' to disable the check entirely (trusted proxy only).",
    ),
    read_only: bool = typer.Option(
        False, "--read-only",
        help="Opt out of default read+write mode and expose only read tools.",
    ),
):
    """Serve the MCP server over HTTP so remote/cloud agents connect by URL.

    Prints the exact URL + config to register in your agent. Pair with the
    OpenSearch backend for a shared, multi-writer brain.
    """
    from open_index.mcp_config import advertised_urls, mask_token
    from open_index.mcp_server import serve_http

    root = Path(brain).resolve()
    b = _open_brain(str(root))
    mode = "read-only" if read_only else "read+write"

    typer.secho(
        f"open-index · brain '{b.config.name}' · {mode} · "
        f"search backend: {b.config.search.backend}",
        fg=typer.colors.GREEN, bold=True,
    )
    typer.echo(f"  listening on {host}:{port}")

    urls = advertised_urls(host, port, public_url=public_url)
    typer.echo("")
    for label, endpoint in urls:
        typer.secho(f"  {label:<22} {endpoint}" if label else f"  {endpoint}",
                    fg=typer.colors.CYAN)

    typer.echo("")
    if token:
        typer.echo(f"  auth: Authorization: Bearer {mask_token(token)}")
    else:
        typer.secho(
            "  auth: NONE — anyone who can reach this port can "
            + ("read" if read_only else "read AND WRITE")
            + " the brain.\n        Set --token / OPEN_INDEX_TOKEN before exposing it.",
            fg=typer.colors.YELLOW,
        )

    # The whole point: hand the user something they can paste, not a bind address.
    connect_url = urls[-1][1]
    hint = f"  agent config:  open-index mcp-config --url {connect_url}"
    if token:
        hint += " --token $OPEN_INDEX_TOKEN"
    typer.echo(hint)
    typer.echo("")

    serve_http(str(root), host=host, port=port, token=token, read_only=read_only,
               warn_unauthenticated=False, public_url=public_url,
               allowed_hosts=list(allowed_host or []))


if __name__ == "__main__":  # pragma: no cover - module entry point
    app()
