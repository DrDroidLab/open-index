"""droid-brain CLI: one command to create, browse and serve a brain.

    droid-brain                      # open the UI (most recent brain, or create one)
    droid-brain new acme --seed-demo # create a brain with demo entities
    droid-brain list                 # list brains
    droid-brain search acme "kafka"  # query a brain from the terminal
    droid-brain ui [acme]            # open the Streamlit UI
    droid-brain mcp [acme]           # serve the brain to any LLM over MCP (stdio)
    droid-brain extract acme --demo  # pull entities from (fake) MCP servers into the brain
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="droid-brain",
        description="Build and serve a local knowledge brain for your agents. No external services required.",
    )
    sub = parser.add_subparsers(dest="command")

    p_ui = sub.add_parser("ui", help="Open the brain UI (Streamlit)")
    p_ui.add_argument("brain", nargs="?", help="Brain to open (default: most recent)")

    p_new = sub.add_parser("new", help="Create a new brain")
    p_new.add_argument("name", help="Name of the brain (this is its index)")
    p_new.add_argument("--description", default="", help="What this brain is for")
    p_new.add_argument("--seed-demo", action="store_true", help="Load demo doc_types and entities")
    p_new.add_argument("--open", action="store_true", help="Open the UI after creating")

    sub.add_parser("list", help="List existing brains")

    p_search = sub.add_parser("search", help="Search a brain from the terminal")
    p_search.add_argument("brain")
    p_search.add_argument("query")
    p_search.add_argument("--type", dest="doc_type", default=None, help="Filter by doc_type")
    p_search.add_argument("--limit", type=int, default=10)

    p_mcp = sub.add_parser("mcp", help="Run an MCP server (stdio) for a brain")
    p_mcp.add_argument("brain", nargs="?", help="Brain to serve (default: most recent)")

    p_extract = sub.add_parser("extract", help="Extract entities from MCP servers into a brain")
    p_extract.add_argument("brain")
    p_extract.add_argument("config", nargs="?", help="JSON config listing sources (MCP server commands + tool specs)")
    p_extract.add_argument("--demo", action="store_true", help="Use the bundled fake MCP servers (grafana/github/aws)")

    args = parser.parse_args(argv)

    if args.command in (None, "ui"):
        brain = getattr(args, "brain", None) or store.most_recent_brain()
        return _launch_ui(brain)
    if args.command == "new":
        return _cmd_new(args)
    if args.command == "list":
        return _cmd_list()
    if args.command == "search":
        return _cmd_search(args)
    if args.command == "mcp":
        return _cmd_mcp(args)
    if args.command == "extract":
        return _cmd_extract(args)
    parser.print_help()
    return 1


def _launch_ui(brain: str | None) -> int:
    from streamlit.web import cli as stcli

    app_path = str(Path(__file__).with_name("ui.py"))
    # Local tool: allow serving behind a reverse proxy / SSH tunnel regardless of install location.
    sys.argv = [
        "streamlit", "run", app_path,
        "--server.enableCORS", "false",
        "--server.enableXsrfProtection", "false",
    ] + (["--", brain] if brain else [])
    return stcli.main()


def _cmd_new(args: argparse.Namespace) -> int:
    seeded = None
    try:
        brain = store.create_brain(args.name, description=args.description)
        try:
            if args.seed_demo:
                from .seed import seed_demo

                seeded = seed_demo(brain)
        finally:
            brain.close()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    suffix = f" (seeded {seeded['entities']} demo entities across {seeded['doc_types']} doc_types)" if seeded else ""
    print(f"Created brain '{args.name}' at {store.brain_path(args.name)}{suffix}")
    print(f"Next: droid-brain ui {args.name}   # manage doc_types and entities")
    print(f"      droid-brain mcp {args.name}  # serve it to any LLM over MCP")
    if args.open:
        return _launch_ui(args.name)
    return 0


def _cmd_list() -> int:
    brains = store.list_brains()
    if not brains:
        print("No brains yet. Create one with: droid-brain new <name>")
        return 0
    for b in brains:
        try:
            with store.open_brain(b["name"]) as brain:
                doc_types = brain.list_doc_types()
                entities = sum(dt["entities"] for dt in doc_types)
        except (ValueError, sqlite3.Error):
            doc_types, entities = [], 0
        print(f"{b['name']:<24} {len(doc_types):>3} doc_types  {entities:>4} entities  {b['path']}")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    try:
        with store.open_brain(args.brain) as brain:
            results = brain.search(args.query, doc_type=args.doc_type, limit=args.limit)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not results:
        print("No results.")
        return 0
    for r in results:
        score = f"  score={r['score']}" if "score" in r else ""
        print(f"[{r['doc_type']}] {r['name']}{score}")
        print(f"    {json.dumps(r['data'])}")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    brain = args.brain or store.most_recent_brain()
    if not brain:
        print("error: no brains yet. Create one with: droid-brain new <name>", file=sys.stderr)
        return 1
    from .mcp_server import run

    try:
        run(brain)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    from . import extract as extract_mod

    sources: Any = extract_mod.DEMO_SOURCES if args.demo else None
    if sources is None:
        if not args.config:
            print("error: pass a config.json or --demo", file=sys.stderr)
            return 1
        try:
            with open(args.config) as f:
                sources = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: cannot read config: {e}", file=sys.stderr)
            return 1
    if not isinstance(sources, list) or not all(isinstance(s, dict) for s in sources):
        print("error: config must be a JSON list of source objects", file=sys.stderr)
        return 1
    try:
        with store.open_brain(args.brain) as brain:
            summary = extract_mod.extract(brain, sources)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    breakdown = ", ".join(f"{count} {doc_type}" for doc_type, count in sorted(summary["by_doc_type"].items()))
    skipped = f", {summary['skipped']} item(s) skipped (no usable name)" if summary.get("skipped") else ""
    print(f"Extracted {summary['entities']} entities ({breakdown}) from {summary['sources']} MCP server(s) into '{args.brain}'{skipped}")
    for error in summary.get("errors", []):
        print(f"warning: source failed: {error}", file=sys.stderr)
    print(f"Next: droid-brain search {args.brain} \"<query>\"   # or: droid-brain ui {args.brain}")
    return 1 if summary.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
