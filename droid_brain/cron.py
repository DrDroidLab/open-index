"""Cron scheduler for Droid Brain connectors.

Usage:
  droid-brain connector add <name> --mcp-cmd ... --tool ... --brain ... --doctype ... --field-mapping '...' [--cron "0 */6 * * *"]
  droid-brain connector list
  droid-brain connector run <name>
  droid-brain cron                                    # start the scheduler
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from droid_brain.core import DEFAULT_DB_PATH
from droid_brain.connectors import Connector, extract_assets, extract_from_handlers


def _cron_db_path() -> str:
    return str(Path(DEFAULT_DB_PATH).parent / "connectors.db")


class CronManager:
    """Manages connector configurations and scheduled execution."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _cron_db_path()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS connectors (
                name TEXT PRIMARY KEY,
                config TEXT NOT NULL,
                cron_expr TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                last_run TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connector_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                entities_created INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                error TEXT,
                FOREIGN KEY (connector_name) REFERENCES connectors(name)
            );
        """)
        self._conn.commit()

    def add(
        self,
        name: str,
        mcp_command: str,
        tool_name: str,
        brain_name: str,
        doc_type: str,
        field_mapping: dict[str, str],
        tool_arguments: Optional[dict] = None,
        cron_expr: str = "",
        transform: Optional[str] = None,
        handler_path: Optional[str] = None,
    ) -> dict:
        config = {
            "mcp_command": mcp_command,
            "tool_name": tool_name,
            "brain_name": brain_name,
            "doc_type": doc_type,
            "field_mapping": field_mapping,
            "tool_arguments": tool_arguments,
            "transform": transform,
            "handler_path": handler_path,
        }
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR REPLACE INTO connectors (name, config, cron_expr, created_at) VALUES (?, ?, ?, ?)",
            (name, json.dumps(config), cron_expr, now),
        )
        self._conn.commit()
        return {"name": name, "cron": cron_expr}

    def list(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM connectors ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM connectors WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def remove(self, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM connectors WHERE name = ?", (name,))
        self._conn.commit()
        return cur.rowcount > 0

    async def run_connector(self, name: str) -> dict:
        """Run a connector immediately and record the run.

        If a handler_path is configured (e.g. 'examples.github_mcp:list_repos'),
        the handler is imported and called in-process — much faster than spawning
        a subprocess. Falls back to subprocess MCP for bare mcp_command connectors.
        """
        row = self.get(name)
        if not row:
            raise ValueError(f"Connector '{name}' not found.")

        config = json.loads(row["config"])
        handler_path = config.get("handler_path")

        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO runs (connector_name, started_at, status) VALUES (?, ?, 'running')",
            (name, now),
        )
        run_id = cur.lastrowid
        self._conn.commit()

        try:
            if handler_path:
                result = await self._run_via_handler(config)
            else:
                connector = Connector(
                    name=name,
                    mcp_command=config["mcp_command"],
                    tool_name=config["tool_name"],
                    brain_name=config["brain_name"],
                    doc_type=config["doc_type"],
                    field_mapping=config["field_mapping"],
                    tool_arguments=config.get("tool_arguments"),
                    transform=config.get("transform"),
                )
                result = await extract_assets(connector)

            finished = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "UPDATE runs SET finished_at = ?, entities_created = ?, status = 'success' WHERE id = ?",
                (finished, result["entities_created"], run_id),
            )
            self._conn.execute(
                "UPDATE connectors SET last_run = ? WHERE name = ?",
                (finished, name),
            )
            self._conn.commit()
            return result
        except Exception as exc:
            finished = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "UPDATE runs SET finished_at = ?, status = 'failed', error = ? WHERE id = ?",
                (finished, str(exc), run_id),
            )
            self._conn.commit()
            raise

    @staticmethod
    async def _run_via_handler(config: dict) -> dict:
        """Import a handler by dotted path and run extraction in-process."""
        handler_path = config["handler_path"]  # e.g. "examples.github_mcp:list_repos"
        module_path, func_name = handler_path.split(":", 1)
        mod = importlib.import_module(module_path.replace("/", "."))
        handler = getattr(mod, func_name)

        tool_handlers = {config["tool_name"]: handler}
        return await extract_from_handlers(
            tool_handlers=tool_handlers,
            tool_name=config["tool_name"],
            arguments=config.get("tool_arguments") or {},
            brain_name=config["brain_name"],
            doc_type=config["doc_type"],
            field_mapping=config["field_mapping"],
            transform=config.get("transform"),
        )

    def get_runs(self, name: str, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE connector_name = ? ORDER BY started_at DESC LIMIT ?",
            (name, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    async def start_scheduler(self) -> None:
        """Start the cron scheduler. Runs until cancelled."""
        print("🕐 Droid Brain cron scheduler started.")
        print(f"   {len(self.list())} connectors registered.")

        while True:
            connectors = self.list()
            now = datetime.now(timezone.utc)

            for c in connectors:
                if not c["cron_expr"] or not c["enabled"]:
                    continue
                if self._should_run(c, now):
                    print(f"▶️  Running connector '{c['name']}' ({now.strftime('%H:%M:%S')})")
                    try:
                        result = await self.run_connector(c["name"])
                        print(f"   ✅ {result['entities_created']} entities created")
                    except Exception as exc:
                        print(f"   ❌ Failed: {exc}")

            await asyncio.sleep(60)  # check every minute

    @staticmethod
    def _should_run(connector: dict, now: datetime) -> bool:
        """Check if a connector should run based on its cron expression."""
        expr = connector["cron_expr"]
        last_run = connector.get("last_run")

        # Simple interval-based scheduling: "every_Nh" or standard cron
        if expr.startswith("every_"):
            # every_6h, every_30m, every_1h
            unit = expr.split("_")[1]
            if unit.endswith("h"):
                interval = int(unit[:-1]) * 3600
            elif unit.endswith("m"):
                interval = int(unit[:-1]) * 60
            else:
                return False

            if last_run:
                last = datetime.fromisoformat(last_run)
                return (now - last).total_seconds() >= interval
            return True  # never run — run now

        # Try standard cron parsing (minute hour day month weekday)
        if last_run:
            last = datetime.fromisoformat(last_run)
            try:
                return _cron_matches(expr, now, last)
            except Exception:
                return False
        return True  # never run — run now


def _cron_matches(expr: str, now: datetime, last_run: datetime) -> bool:
    """Check if a standard 5-field cron expression fires between last_run and now."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False

    minute, hour, day, month, weekday = parts

    # Only compare at minute granularity
    last_minute = last_run.replace(second=0, microsecond=0)
    current_minute = now.replace(second=0, microsecond=0)

    if current_minute <= last_minute:
        return False

    # Check if now matches the cron pattern
    return (
        _field_matches(minute, now.minute)
        and _field_matches(hour, now.hour)
        and _field_matches(day, now.day)
        and _field_matches(month, now.month)
        and _field_matches(weekday, (now.weekday() + 1) % 7)  # 0=Sun→7 in cron
    )


def _field_matches(pattern: str, value: int) -> bool:
    if pattern == "*":
        return True
    for part in pattern.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            if int(lo) <= value <= int(hi):
                return True
        elif "/" in part:
            base, step = part.split("/")
            if base == "*":
                base_val = 0
            else:
                base_val = int(base)
            if value >= base_val and (value - base_val) % int(step) == 0:
                return True
        else:
            if int(part) == value:
                return True
    return False
