"""The Brain handle — one object the CLI, MCP server, and UI all talk to.

Opening a brain loads its config + doc_type schemas and connects the configured
search backend. `index()` (re)loads entities from disk into the backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from open_index.config import BrainConfig, iter_entity_files, load_brain_config
from open_index.models import Entity
from open_index.schema import DocType
from open_index.storage import get_backend
from open_index.storage.base import SearchBackend, SearchResults


class Brain:
    def __init__(self, config: BrainConfig, backend: Optional[SearchBackend] = None):
        self.config = config
        self.backend: SearchBackend = backend or get_backend(config)
        self.backend.ensure_schema(config.doc_types)

    @classmethod
    def open(cls, brain_dir: str | Path) -> "Brain":
        return cls(load_brain_config(brain_dir))

    # -- ingestion -------------------------------------------------------------

    def validate_entity(self, entity: Entity) -> list[str]:
        """Return validation errors for an entity against its doc_type schema.

        Includes a light relationship check: when an edge uses a *declared*
        relationship whose `target_doc_type` is set, and the target entity
        already exists with a different doc_type, that's flagged (a dashboard
        where a service was expected). Forward references to not-yet-created
        entities are allowed."""
        errors: list[str] = []
        dt = self.config.doc_type(entity.doc_type)
        if dt is None:
            errors.append(f"unknown doc_type '{entity.doc_type}'")
            return errors
        errors.extend(dt.validate_entity_fields(entity.fields))

        for rel in entity.related_to:
            spec = dt.relationship(rel.relationship_edge_meaning)
            if spec is None or spec.target_doc_type is None:
                continue
            target = self.backend.get_entity(rel.target)
            if target is not None and target.doc_type != spec.target_doc_type:
                errors.append(
                    f"relationship '{rel.relationship_edge_meaning}' expects a "
                    f"'{spec.target_doc_type}' target, but {rel.target} is a "
                    f"'{target.doc_type}'"
                )
        return errors

    def add_entity(self, entity: Entity, validate: bool = True) -> None:
        if validate:
            errors = self.validate_entity(entity)
            if errors:
                raise ValueError(
                    f"invalid entity {entity.id}: " + "; ".join(errors)
                )
        self.backend.upsert_entity(entity, self.config.doc_type(entity.doc_type))

    # -- writes that persist to disk (files stay the git source of truth) ------

    def create_doc_type(self, doc_type: DocType, overwrite: bool = False) -> Path:
        """Register a new doc_type, writing its schema to doc_types/<name>.yaml.

        This is how an agent (or `add-doc-type`) defines a concept at runtime:
        the YAML file is the durable, git-tracked artifact; the in-memory config
        and backend are updated so it's immediately usable."""
        from open_index.config import write_doc_type

        if self.config.root is None:
            raise RuntimeError("brain has no directory; cannot persist doc_type")
        if doc_type.doc_type in self.config.doc_types and not overwrite:
            raise ValueError(f"doc_type '{doc_type.doc_type}' already exists")
        path = write_doc_type(self.config.root, doc_type)
        self.config.doc_types[doc_type.doc_type] = doc_type
        self.backend.ensure_schema(self.config.doc_types)
        return path

    def entity_path(self, entity: Entity) -> Path:
        """Where an entity's JSON file lives: entities/<doc_type>/<slug>.json."""
        assert self.config.root is not None
        slug = entity.id.split(":", 1)[1]
        return self.config.root / "entities" / entity.doc_type / f"{slug}.json"

    def put_entity(
        self, entity: Entity, validate: bool = True, persist: Optional[bool] = None
    ) -> Optional[Path]:
        """Validate, index, and (for file-backed types) persist an entity.

        The single write path every rung funnels through — manual JSON, agent via
        MCP, CSV import, connector-pulled. Whether a JSON file is written follows
        the doc_type's `storage` policy: `file` writes it (git source of truth),
        `index` does not (DB is the source of truth). Pass `persist` to override.
        Returns the file path when written, else None."""
        import json

        if validate:
            errors = self.validate_entity(entity)
            if errors:
                raise ValueError(f"invalid entity {entity.id}: " + "; ".join(errors))

        dt = self.config.doc_type(entity.doc_type)
        if persist is None:
            persist = dt is not None and dt.storage == "file"

        self.backend.upsert_entity(entity, dt)

        if persist and self.config.root is not None:
            path = self.entity_path(entity)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entity.to_json(), indent=2, ensure_ascii=False) + "\n")
            return path
        return None

    def _file_backed_types(self) -> set[str]:
        return {n for n, dt in self.config.doc_types.items() if dt.storage == "file"}

    def index(self) -> int:
        """Reconcile file-backed entities from disk into the backend.

        Only doc_types with `storage: file` are (re)loaded from JSON — their files
        are the source of truth, so this deletes their current rows and reloads.
        `storage: index` types live only in the DB (written by connectors/agents)
        and are left untouched. Returns the number of file entities loaded."""
        import json

        if self.config.root is None:
            return 0

        file_types = self._file_backed_types()
        # Clear only the file-backed types so we can pick up edits/deletions,
        # without destroying index-backed entities that have no files.
        self.backend.delete_by_doc_type(list(file_types))

        count = 0
        for path in iter_entity_files(self.config.root):
            raw = json.loads(Path(path).read_text())
            records = raw if isinstance(raw, list) else [raw]
            for rec in records:
                entity = Entity.from_dict(rec)
                # Ignore stray files for index-backed types — the DB owns those.
                if entity.doc_type in file_types or entity.doc_type not in self.config.doc_types:
                    self.add_entity(entity, validate=True)
                    count += 1
        return count

    # -- queries ---------------------------------------------------------------

    def search(
        self,
        query: Optional[str] = None,
        doc_types: Optional[list[str]] = None,
        limit: int = 20,
        counts_only: bool = False,
        semantic_weight: Optional[float] = None,
    ) -> SearchResults:
        return self.backend.search(query, doc_types, limit, counts_only,
                                   semantic_weight=semantic_weight)

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        return self.backend.get_entity(entity_id)

    def counts(self) -> dict[str, int]:
        return self.backend.counts()

    def reembed(self) -> None:
        """Recompute embeddings for every stored entity."""
        self.backend.reembed()

    def observed_relationships(self, doc_type: str) -> dict[str, int]:
        """Distinct outgoing edge meanings actually used by a doc_type's entities,
        with how many edges use each. Surfaces the real correlation vocabulary."""
        meanings: dict[str, int] = {}
        for e in self.backend.all_entities([doc_type]):
            for rel in e.related_to:
                m = rel.relationship_edge_meaning or "(unlabeled)"
                meanings[m] = meanings.get(m, 0) + 1
        return meanings

    def navigation_guidelines(self, examples_per_type: int = 3) -> str:
        """A markdown guide telling an agent how to navigate *this* brain.

        Mirrors the reference `build_navigation_guidelines`: enumerate every
        doc_type with its count, fields (marking searchable ones + boost),
        example entities, and a concrete search call — plus how to write back.
        This is the read tool an agent calls first to orient itself."""
        counts = self.counts()
        total = sum(counts.values())
        lines: list[str] = []
        lines.append(f"# {self.config.name} — Context Brain Navigation Guide")
        if self.config.description:
            lines.append(f"\n{self.config.description}")
        lines.append(
            f"\nThis brain holds **{total} entities** across "
            f"**{len(self.config.doc_types)} doc_types**. Each entity may link to "
            "others via `related_to` edges (`target` + `relationship_edge_meaning`)."
        )

        lines.append("\n## How to read")
        lines.append(
            "- `search_brain(query=\"...\", doc_types=[...], limit=N)` — search/filter entities.\n"
            "- `get_entity(entity_id)` — one entity plus its incoming/outgoing relationships.\n"
            "- Start with a query; filter by `doc_types` to narrow."
        )
        lines.append("\n## How to write")
        lines.append(
            "- `put_entity(...)` — add or update an entity (validated, persisted to disk).\n"
            "- `create_doc_type(...)` — define a new concept when none fits.\n"
            "- Prefer reusing an existing doc_type over inventing one."
        )

        lines.append("\n## Doc types")
        for name, dt in self.config.doc_types.items():
            count = counts.get(name, 0)
            lines.append(f"\n### {name} ({count})")
            if dt.description:
                lines.append(dt.description)
            searchable = [f for f in dt.fields if f.search != "none"]
            if searchable:
                field_bits = ", ".join(
                    f"`{f.name}`"
                    + (f" (boost {f.boost:g})" if f.boost != 1 else "")
                    for f in sorted(searchable, key=lambda x: -x.boost)
                )
                lines.append(f"- **Searchable:** {field_bits}")
            # Relationships — declared (the expected vocabulary) + observed (in use).
            if dt.relationships:
                decl = ", ".join(
                    f"`{r.name}`" + (f" → {r.target_doc_type}" if r.target_doc_type else "")
                    for r in dt.relationships
                )
                lines.append(f"- **Relationships (declared):** {decl}")
            observed = self.observed_relationships(name)
            if observed:
                obs = ", ".join(f'"{m}" ×{n}' for m, n in sorted(observed.items(), key=lambda x: -x[1]))
                lines.append(f"- **Relationships (in use):** {obs}")
            examples = self.backend.all_entities([name])[:examples_per_type]
            if examples:
                lines.append("- **Examples:** " + ", ".join(f"`{e.id}`" for e in examples))
            lines.append(f'- **Query:** `search_brain(query="...", doc_types=["{name}"])`')

        return "\n".join(lines)

    def structure(self, examples_per_type: int = 3) -> dict[str, Any]:
        """A textual/structured description of the brain: doc_types, their
        schemas, counts, and a few example entities each. Backs `brain_structure`."""
        counts = self.counts()
        doc_types: list[dict[str, Any]] = []
        for name, dt in self.config.doc_types.items():
            examples = [
                {"id": e.id, "name": e.name}
                for e in self.backend.all_entities([name])[:examples_per_type]
            ]
            doc_types.append(
                {
                    "doc_type": name,
                    "description": dt.description,
                    "storage": dt.storage,
                    "count": counts.get(name, 0),
                    "fields": [
                        {
                            "name": f.name,
                            "type": f.type,
                            "search": f.search,
                            "boost": f.boost,
                        }
                        for f in dt.fields
                    ],
                    "relationships": {
                        "declared": [
                            {"name": r.name, "target_doc_type": r.target_doc_type}
                            for r in dt.relationships
                        ],
                        "observed": self.observed_relationships(name),
                    },
                    "examples": examples,
                }
            )
        return {
            "name": self.config.name,
            "description": self.config.description,
            "total_entities": sum(counts.values()),
            "doc_types": doc_types,
        }
