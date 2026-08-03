"""When a connector should run, and remembering when it last did.

Deliberately dependency-free: schedules are human intervals ("6h", "30m", "1d",
"1w"), the keyword "manual" (never auto-run), or a plain number of seconds. This
covers the "run every N" intent without pulling in a cron parser. For exact
wall-clock cron, point your system crontab / CI at `droid-brain run` (or
`droid-brain ingest <name>`) — droid-brain tracks *whether a connector is due*,
and lets your existing scheduler drive the *clock*.

Last-run state lives in a gitignored JSON file in the brain dir, so it survives
across `run` invocations without coupling to any storage backend.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_KEYWORDS = {
    "manual": None,
    "always": 0,
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
}

_STATE_FILE = ".droid_brain_state.json"


def parse_schedule(schedule: str | int | None) -> Optional[int]:
    """Return the interval in seconds, or None for 'manual'/unset.

    Accepts: "manual"|"hourly"|"daily"|"weekly", "6h"/"30m"/"1d"/"2w", or an int.
    """
    if schedule is None:
        return None
    if isinstance(schedule, (int, float)):
        return int(schedule)
    s = str(schedule).strip().lower()
    if s in _KEYWORDS:
        return _KEYWORDS[s]
    # Interval form like "6h": trailing unit char, leading number.
    unit = s[-1]
    if unit in _UNITS and s[:-1].isdigit():
        return int(s[:-1]) * _UNITS[unit]
    raise ValueError(
        f"unrecognized schedule {schedule!r}; use 'manual', 'daily', or '6h'/'30m'/'1d'/'1w'"
    )


def is_due(schedule: str | int | None, last_run: Optional[str], now: datetime) -> bool:
    """Whether a connector with this schedule is due to run at `now`."""
    interval = parse_schedule(schedule)
    if interval is None:  # manual
        return False
    if last_run is None:  # never run
        return True
    last = datetime.fromisoformat(last_run)
    return (now - last).total_seconds() >= interval


class RunState:
    """Tracks last-run timestamps per connector in a JSON file."""

    def __init__(self, brain_root: Path):
        self.path = brain_root / _STATE_FILE
        self._data: dict = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def last_run(self, name: str) -> Optional[str]:
        return self._data.get(name, {}).get("last_run")

    def record(self, name: str, now: datetime, status: str, count: int) -> None:
        self._data[name] = {
            "last_run": now.isoformat(),
            "last_status": status,
            "last_count": count,
        }
        self.path.write_text(json.dumps(self._data, indent=2))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
