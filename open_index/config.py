"""Loading a brain from disk: brain.yaml + doc_types/*.yaml.

A brain lives in a directory:

    my-brain/
      brain.yaml            # name, description, storage + search backend choice
      doc_types/*.yaml      # one schema file per doc_type
      entities/**/*.json    # seed entities (optional; loaded on `index`)
      connectors/*.py       # ingestion scripts (optional)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from open_index.schema import DocType

logger = logging.getLogger("open_index.config")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: Any) -> Any:
    """Expand ${VAR} references (in strings, lists, dicts) from the environment.

    Keeps secrets — OpenSearch passwords, MCP tokens — out of config/code."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


class StorageConfig(BaseModel):
    """Where SQLite keeps its file. That is all this section controls.

    There is deliberately no `backend` key here: which engine stores and
    searches the brain is one decision, and it lives in `search.backend`. An
    earlier version accepted `storage.backend` and silently ignored it, which
    read as if storage and search could be chosen independently. Old files still
    load — `load_brain_config` warns instead of failing."""

    path: str = "./brain.db"


class SearchConfig(BaseModel):
    backend: str = "sqlite"
    # -- OpenSearch options (used when backend == "opensearch"). Secrets may be
    #    ${ENV} references, resolved at connect time. --
    hosts: list[str] = Field(default_factory=lambda: ["http://localhost:9200"])
    index: Optional[str] = None          # defaults to open_index_<name>
    username: Optional[str] = None
    password: Optional[str] = None
    use_ssl: bool = False
    verify_certs: bool = False
    # -- Semantic search options --
    # Weight of the semantic/vector score in the final hybrid ranking. 0 = keyword-only,
    # 1 = semantic-only. Default is an even 0.5 blend.
    # Keyword matches are the primary signal; semantic similarity breaks the
    # "right words weren't used" cases. Default weights keyword 70/30.
    semantic_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    # Override the default fastembed model. Leave unset for BAAI/bge-small-en-v1.5.
    embedding_model: Optional[str] = None


class BrainConfig(BaseModel):
    """Parsed brain.yaml plus the loaded doc_type schemas."""

    name: str
    description: str = ""
    storage: StorageConfig = Field(default_factory=StorageConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    # Populated from doc_types/*.yaml, keyed by doc_type name.
    doc_types: dict[str, DocType] = Field(default_factory=dict)

    # The directory this config was loaded from (not serialized).
    root: Optional[Path] = Field(default=None, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    def doc_type(self, name: str) -> Optional[DocType]:
        return self.doc_types.get(name)

    def db_path(self) -> Path:
        """Absolute path to the storage file, resolved against the brain root."""
        p = Path(self.storage.path)
        if not p.is_absolute() and self.root is not None:
            p = self.root / p
        return p


"""Environment overrides applied after brain.yaml is parsed.

These exist so one brain directory can run against different backends without
editing (and accidentally committing) a change to brain.yaml — the container
case, where the image is fixed and only the environment differs. Keep this list
short and explicit: a blanket ${ENV} expansion over the whole file would turn an
unset variable into an empty string and fail validation in a confusing way.
"""
_ENV_OVERRIDES = {
    "OPEN_INDEX_SEARCH_BACKEND": ("search", "backend"),
    "OPEN_INDEX_DB_PATH": ("storage", "path"),
    "OPEN_INDEX_OPENSEARCH_INDEX": ("search", "index"),
}

# Comma-separated list → search.hosts.
_ENV_HOSTS = "OPEN_INDEX_OPENSEARCH_HOSTS"


def apply_env_overrides(config: "BrainConfig") -> "BrainConfig":
    """Override backend selection from the environment. Mutates and returns."""
    for var, (section, key) in _ENV_OVERRIDES.items():
        value = os.environ.get(var)
        if value:
            setattr(getattr(config, section), key, value)

    hosts = os.environ.get(_ENV_HOSTS)
    if hosts:
        config.search.hosts = [h.strip() for h in hosts.split(",") if h.strip()]

    if config.search.backend not in ("sqlite", "opensearch"):
        raise ValueError(
            f"unknown search backend {config.search.backend!r} "
            "(expected 'sqlite' or 'opensearch')"
        )
    return config


def load_brain_config(brain_dir: str | Path) -> BrainConfig:
    """Load brain.yaml and every doc_types/*.yaml under `brain_dir`."""
    root = Path(brain_dir).expanduser().resolve()
    brain_yaml = root / "brain.yaml"
    if not brain_yaml.exists():
        raise FileNotFoundError(
            f"no brain.yaml found in {root} — run `open-index init` first"
        )

    raw = yaml.safe_load(brain_yaml.read_text()) or {}

    # `storage.backend` was never read; warn rather than ignore, so a brain that
    # looks like it selects a backend there learns where the real knob is.
    storage_raw = raw.get("storage")
    if isinstance(storage_raw, dict) and "backend" in storage_raw:
        logger.warning(
            "%s: 'storage.backend' is not used and will be ignored — the engine "
            "is chosen by 'search.backend' (currently %r). Remove it to avoid "
            "confusion.",
            brain_yaml,
            (raw.get("search") or {}).get("backend", "sqlite"),
        )

    config = BrainConfig.model_validate(raw)
    apply_env_overrides(config)
    config.root = root

    doc_types_dir = root / "doc_types"
    if doc_types_dir.is_dir():
        for f in sorted(doc_types_dir.glob("*.yaml")):
            data = yaml.safe_load(f.read_text()) or {}
            dt = DocType.from_dict(data)
            if dt.doc_type in config.doc_types:
                raise ValueError(f"duplicate doc_type '{dt.doc_type}' (in {f.name})")
            config.doc_types[dt.doc_type] = dt

    return config


def doc_type_to_yaml_dict(dt: DocType) -> dict:
    """Serialize a DocType back to the nested schema shape used on disk."""
    fields = []
    for f in dt.fields:
        entry = {
            "name": f.name,
            "type": f.type,
            "processing": f.processing,
            "search": f.search,
            "boost": f.boost,
        }
        if f.required:
            entry["required"] = True
        if f.description:
            entry["description"] = f.description
        fields.append(entry)
    out = {
        "doc_type": dt.doc_type,
        "description": dt.description,
        "storage": dt.storage,
        "display": {"label_field": dt.display.label_field, "color": dt.display.color},
        "schema": {"fields": fields},
    }
    if dt.relationships:
        out["relationships"] = [
            {k: v for k, v in {
                "name": r.name,
                "target_doc_type": r.target_doc_type,
                "description": r.description,
            }.items() if v is not None}
            for r in dt.relationships
        ]
    return out


def write_doc_type(brain_root: Path, dt: DocType) -> Path:
    """Persist a doc_type to <brain>/doc_types/<name>.yaml. Returns the path."""
    dt_dir = brain_root / "doc_types"
    dt_dir.mkdir(parents=True, exist_ok=True)
    path = dt_dir / f"{dt.doc_type}.yaml"
    path.write_text(
        yaml.safe_dump(doc_type_to_yaml_dict(dt), sort_keys=False, default_flow_style=False)
    )
    return path


def discover_brains(root: str | Path) -> dict[str, Path]:
    """Every brain directory directly under `root`, keyed by directory name.

    A "brain" is any subdirectory holding a brain.yaml, so a root can sit
    alongside unrelated folders without confusing anything.
    """
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise FileNotFoundError(f"no such directory: {base}")
    return {
        child.name: child
        for child in sorted(base.iterdir())
        if (child / "brain.yaml").exists()
    }


def iter_entity_files(brain_dir: str | Path):
    """Yield every entities/**/*.json path under the brain directory."""
    root = Path(brain_dir).expanduser().resolve()
    entities_dir = root / "entities"
    if entities_dir.is_dir():
        yield from sorted(entities_dir.rglob("*.json"))
