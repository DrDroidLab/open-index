"""Discover, instantiate, and run connectors against a brain.

Connectors live in `<brain>/connectors/*.py`. Any subclass of `Connector` in
those files is discoverable by its `name` attribute. Running a connector opens
an MCP client (if it declares `mcp_url`), executes every `extract_*` method, and
upserts the produced entities into the brain.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from open_index.brain import Brain
from open_index.connectors.base import Connector
from open_index.connectors.mcp_client import McpClient
from open_index.scheduling import RunState, is_due, utcnow

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env(value: Any) -> Any:
    """Expand ${VAR} references in strings (and dict values) from the environment.

    Keeps secrets out of connector code — the script says ${LINEAR_TOKEN}, the
    value comes from the environment at run time."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    return value


@dataclass
class IngestResult:
    connector: str
    created: int
    errors: list[str]
    skipped: bool = False


def discover_connectors(brain: Brain) -> dict[str, type[Connector]]:
    """Return {name: ConnectorClass} for every connector in <brain>/connectors."""
    found: dict[str, type[Connector]] = {}
    if brain.config.root is None:
        return found
    conn_dir = brain.config.root / "connectors"
    if not conn_dir.is_dir():
        return found

    for path in sorted(conn_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = _load_module(path)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Connector) and obj is not Connector:
                found[obj.name] = obj
    return found


def _load_module(path: Path):
    import sys

    name = f"open_index_connector_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Register so inspect.getsource() (used by the UI's script viewer) can find it.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_connector(
    brain: Brain,
    connector_cls: type[Connector],
    validate: bool = True,
    **config,
) -> IngestResult:
    """Instantiate one connector, run its extractors, upsert the entities."""
    mcp: Optional[McpClient] = None
    if connector_cls.mcp_url:
        url = _resolve_env(connector_cls.mcp_url)
        headers = _resolve_env(connector_cls.mcp_auth_headers) if connector_cls.mcp_auth_headers else None
        mcp = McpClient(url, headers)

    connector = connector_cls(mcp=mcp, **config)
    created = 0
    errors: list[str] = []
    try:
        for spec in connector.run():
            entity = spec.to_entity()
            try:
                # Land through the same validated write path as everything else;
                # whether a file is written follows the doc_type's storage policy
                # (index-backed types stay DB-only — right for high-volume pulls).
                brain.put_entity(entity, validate=validate)
                created += 1
            except Exception as exc:  # keep going; report per-entity failures
                errors.append(f"{entity.id}: {exc}")
    finally:
        if mcp is not None:
            mcp.close()

    return IngestResult(connector=connector_cls.name, created=created, errors=errors)


def ingest(brain: Brain, name: str, **config) -> IngestResult:
    """Look up a connector by name in the brain and run it."""
    connectors = discover_connectors(brain)
    if name not in connectors:
        available = ", ".join(sorted(connectors)) or "(none)"
        raise KeyError(f"no connector named '{name}'. Available: {available}")
    return run_connector(brain, connectors[name], **config)


def run_due(brain: Brain, force: bool = False) -> list[IngestResult]:
    """Run every connector whose schedule is due (or all, if force).

    Cron-friendly: call this from system cron / CI / a loop. Tracks last-run
    per connector in a gitignored state file so cadence survives invocations."""
    if brain.config.root is None:
        return []
    state = RunState(brain.config.root)
    now = utcnow()
    results: list[IngestResult] = []
    for name, cls in sorted(discover_connectors(brain).items()):
        due = force or is_due(cls.schedule, state.last_run(name), now)
        if not due:
            results.append(IngestResult(connector=name, created=0, errors=[], skipped=True))
            continue
        result = run_connector(brain, cls)
        state.record(name, now, "ok" if not result.errors else "partial", result.created)
        results.append(result)
    return results
