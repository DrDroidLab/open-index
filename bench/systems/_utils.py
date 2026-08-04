"""Internal helpers for brain-backed memory systems."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

import yaml

from bench.config import BENCH_DIR

_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9._-]")
_SPACE_RE = re.compile(r"\s+")


def normalize_slug(value: str) -> str:
    """Normalize a string into a valid droid-brain entity slug.

    Rules: lowercase, spaces/whitespace -> hyphens, strip any character outside
    `[a-zA-Z0-9._-]`. If the result is empty, return "x".
    """
    value = str(value).lower().strip()
    value = _SPACE_RE.sub("-", value)
    value = _SLUG_INVALID.sub("", value)
    value = value.strip("-.")
    if not value:
        return "x"
    return value


def copy_brain_config(src_dir: Path, dest_dir: Path, db_path: Path | str) -> None:
    """Copy brain.yaml + doc_types/ into a fresh brain directory and rewrite the DB path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "doc_types").mkdir(parents=True, exist_ok=True)

    brain_yaml = yaml.safe_load((src_dir / "brain.yaml").read_text()) or {}
    brain_yaml["storage"] = brain_yaml.get("storage", {})
    brain_yaml["storage"]["backend"] = "sqlite"
    brain_yaml["storage"]["path"] = str(Path(db_path).resolve())
    brain_yaml["search"] = brain_yaml.get("search", {})
    brain_yaml["search"]["backend"] = "sqlite"

    (dest_dir / "brain.yaml").write_text(yaml.safe_dump(brain_yaml, sort_keys=False))

    for f in sorted((src_dir / "doc_types").glob("*.yaml")):
        shutil.copy2(f, dest_dir / "doc_types" / f.name)


def make_temp_brain(config_name: str) -> Path:
    """Create a temporary brain directory seeded from `bench/configs/<name>`."""
    tmpdir = Path(tempfile.mkdtemp(prefix=f"bench_{config_name}_"))
    src = BENCH_DIR / "configs" / config_name
    db_path = tmpdir / "brain.db"
    copy_brain_config(src, tmpdir, db_path)
    return tmpdir
