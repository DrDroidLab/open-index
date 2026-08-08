"""The Brain handle — one object the CLI, MCP server, and UI all talk to.

Opening a brain loads its config + doc_type schemas and connects the configured
search backend. `index()` (re)loads entities from disk into the backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from dataclasses import dataclass, field

from open_index.config import BrainConfig, iter_entity_files, load_brain_config
from open_index.models import Entity, Provenance
from open_index.schema import DocType
from open_index.storage import get_backend
from open_index.storage.base import SearchBackend, SearchResults


@dataclass
class BulkResult:
    """Outcome of a batch write: what landed, and what didn't and why.

    Partial success is the normal case for an import, so this reports both
    rather than raising — one malformed row should not discard the other 499.
    """

    written: int = 0
    errors: list[str] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return len(self.errors)

    def summary(self) -> str:
        text = f"{self.written} written"
        if self.errors:
            text += f", {self.failed} failed"
        if self.paths:
            text += f", {len(self.paths)} file(s) persisted"
        return text


class Brain:
    def __init__(self, config: BrainConfig, backend: Optional[SearchBackend] = None):
        self.config = config
        # Populated by index(): per-entity failures that did not abort the run.
        # Read this after indexing — a silent partial load is the failure mode
        # this list exists to prevent.
        self.index_errors: list[str] = []
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

    def put_entities(
        self,
        entities: list[Entity],
        validate: bool = True,
        provenance: Optional["Provenance"] = None,
    ) -> "BulkResult":
        """Write many entities in one batch.

        Differs from calling `put_entity` in a loop in two ways that matter at
        volume: the backend writes once (one transaction / one bulk request /
        one embedding call), and a bad row does not abort the run. Invalid
        entities are collected in `BulkResult.errors` and everything else still
        lands — a 500-row import should not be undone by one typo.

        `provenance` is applied to entities that don't carry their own, so a
        caller importing a batch attributes it once instead of per row. An
        entity's own provenance always wins.
        """
        result = BulkResult()
        writable: list[tuple[Entity, Optional[DocType]]] = []

        for entity in entities:
            if provenance is not None and entity.provenance is None:
                entity = entity.model_copy(update={"provenance": provenance})
            if validate:
                errors = self.validate_entity(entity)
                if errors:
                    result.errors.append(f"{entity.id}: {'; '.join(errors)}")
                    continue
            writable.append((entity, self.config.doc_type(entity.doc_type)))

        if not writable:
            return result

        self.backend.upsert_many(writable)
        result.written = len(writable)

        # File-backed types keep JSON on disk as the source of truth; index-backed
        # ones live only in the DB. Same policy as put_entity, applied per row.
        for entity, dt in writable:
            if dt is not None and dt.storage == "file" and self.config.root is not None:
                result.paths.append(self._write_entity_file(entity))
        return result

    def _write_entity_file(self, entity: Entity) -> Path:
        import json

        path = self.entity_path(entity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entity.to_json(), indent=2, ensure_ascii=False) + "\n")
        return path

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
        self.index_errors = []
        for path in iter_entity_files(self.config.root):
            try:
                raw = json.loads(Path(path).read_text())
            except (OSError, ValueError) as exc:
                self.index_errors.append(f"{path}: unreadable ({exc})")
                continue
            records = raw if isinstance(raw, list) else [raw]
            for rec in records:
                # One malformed file must not abort the whole reload. `index` is the
                # recover-from-disk path, so a typo in one entity previously took the
                # entire brain offline with no indication of which file was at fault.
                try:
                    entity = Entity.from_dict(rec)
                    # Ignore stray files for index-backed types — the DB owns those.
                    if (entity.doc_type in file_types
                            or entity.doc_type not in self.config.doc_types):
                        self.add_entity(entity, validate=True)
                        count += 1
                except (ValueError, TypeError, KeyError) as exc:
                    ident = rec.get("id") if isinstance(rec, dict) else "?"
                    self.index_errors.append(f"{path} [{ident}]: {exc}")
        return count

    # -- queries ---------------------------------------------------------------

    def search(
        self,
        query: Optional[str] = None,
        doc_types: Optional[list[str]] = None,
        limit: int = 20,
        counts_only: bool = False,
        semantic_weight: Optional[float] = None,
        min_confidence: float = 0.0,
        as_of: Optional[str] = None,
    ) -> SearchResults:
        """Search, optionally filtered by trust and by validity window.

        `min_confidence` drops claims that cannot clear the floor, INCLUDING
        unattributed ones — see `Entity.trusted`. `as_of` drops claims whose
        validity window excludes that instant. Both default to off so existing
        callers are unaffected.

        Filtering happens after retrieval rather than in the backend query: the
        fields live inside the stored JSON blob, and pushing predicates into two
        backends would buy speed the current scale does not need at the cost of
        the two implementations drifting apart.
        """
        results = self.backend.search(query, doc_types, limit, counts_only,
                                      semantic_weight=semantic_weight)
        if counts_only or (min_confidence <= 0 and as_of is None):
            return results

        kept = []
        for row in results.results:
            raw = row.get("entity")
            if not isinstance(raw, dict):
                kept.append(row)          # nothing to judge on; do not silently drop
                continue
            ent = Entity.from_dict(raw)
            if ent.trusted(min_confidence) and ent.holds_at(as_of):
                kept.append(row)

        # Recompute the aggregates so the map's spoke counts agree with the rows
        # actually returned. A filtered result set with unfiltered counts is a
        # display that disagrees with the store for reasons the reader cannot see.
        counts: dict[str, int] = {}
        for row in kept:
            dt = row.get("doc_type")
            if dt:
                counts[dt] = counts.get(dt, 0) + 1
        results.results = kept
        results.total = len(kept)
        results.doc_type_counts = counts
        return results

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        return self.backend.get_entity(entity_id)

    def counts(self) -> dict[str, int]:
        return self.backend.counts()

    def reembed(self) -> None:
        """Recompute embeddings for every stored entity."""
        self.backend.reembed()

    def provenance_report(self) -> dict[str, Any]:
        """How much of this brain can be audited?

        Surfaces the share of entities carrying an asserter, a confidence and a
        validity window, broken down by doc_type. An agent-written store degrades
        silently without this: nothing distinguishes a measured fact from a guess
        once both are rows, and a bad attribution is served in the same voice as a
        good one. This is the number to watch when agents write at volume.
        """
        by_type: dict[str, dict[str, int]] = {}
        for name in self.config.doc_types:
            ents = self.backend.all_entities([name])
            if not ents:
                continue
            attributed = sum(1 for e in ents
                             if e.provenance is not None and e.provenance.asserted_by)
            scored = sum(1 for e in ents
                         if e.provenance is not None and e.provenance.confidence is not None)
            dated = sum(1 for e in ents if e.valid_from or e.valid_to)
            by_type[name] = {
                "entities": len(ents),
                "attributed": attributed,
                "scored": scored,
                "dated": dated,
                "unattributed": len(ents) - attributed,
            }
        total = sum(v["entities"] for v in by_type.values())
        attributed = sum(v["attributed"] for v in by_type.values())
        return {
            "total_entities": total,
            "attributed": attributed,
            "attributed_pct": round(100 * attributed / total, 1) if total else 0.0,
            "by_doc_type": by_type,
        }

    def observed_relationships(self, doc_type: str) -> dict[str, int]:
        """Distinct outgoing edge meanings actually used by a doc_type's entities,
        with how many edges use each. Surfaces the real correlation vocabulary."""
        meanings: dict[str, int] = {}
        for e in self.backend.all_entities([doc_type]):
            for rel in e.related_to:
                m = rel.relationship_edge_meaning or "(unlabeled)"
                meanings[m] = meanings.get(m, 0) + 1
        return meanings

    def navigation_guidelines(
        self, examples_per_type: int = 3, read_only: bool = False
    ) -> str:
        """A markdown guide telling an agent how to navigate *and author* this brain.

        This is the first tool an agent calls, and for a remote brain it is the
        *only* documentation it will ever see — `CLAUDE.md` and the `edit-brain`
        skill are files on the brain host, invisible over MCP. So this has to be
        self-contained: the model, the id convention, the exact call shapes, and
        the full schema vocabulary, not just an inventory of what exists.

        Set `read_only` to drop the authoring sections on a read-only endpoint,
        where describing write tools that aren't registered only misleads.
        """
        counts = self.counts()
        total = sum(counts.values())
        lines: list[str] = [f"# {self.config.name} — Context Brain Navigation Guide"]
        if self.config.description:
            lines.append(f"\n{self.config.description}")
        lines.append(
            f"\nThis brain holds **{total} entities** across "
            f"**{len(self.config.doc_types)} doc_types**."
        )

        lines.extend(self._guide_model_section())
        lines.extend(self._guide_read_section())

        if not read_only:
            if not self.config.doc_types:
                # An empty brain is exactly when an agent needs the most help and
                # the inventory below tells it nothing. Lead with a bootstrap.
                lines.extend(self._guide_bootstrap_section())
            lines.extend(self._guide_write_section())

        lines.extend(self._guide_doc_types_section(counts, examples_per_type))
        return "\n".join(lines)

    # -- navigation_guidelines sections ----------------------------------------

    @staticmethod
    def _guide_model_section() -> list[str]:
        return [
            "\n## The model (three things)",
            "- **doc_type** — a concept this brain tracks, plus the schema of its fields\n"
            "  (e.g. `service`, `issue`, `customer`).\n"
            "- **entity** — one instance of a doc_type. Its id is **always**\n"
            "  `<doc_type>:<slug>` (e.g. `issue:payment-declined`); the prefix must match\n"
            "  the doc_type or the write is rejected. Allowed characters: `a-z A-Z 0-9 . _ -`.\n"
            "- **relationship** — every entity may link to any other through `related_to`:\n"
            "  `{\"target\": \"<entity_id>\", \"relationship_edge_meaning\": \"<free text>\"}`.\n"
            "  The edges are the point of the brain — an entity with no edges is a note.",
        ]

    @staticmethod
    def _guide_read_section() -> list[str]:
        return [
            "\n## How to read",
            '- `search_brain(query="...", doc_types=[...], limit=N)` — search/filter entities.\n'
            "- `get_entity(entity_id)` — one entity plus its incoming *and* outgoing edges.\n"
            "- Start broad with a query, then narrow with `doc_types`. To enumerate a type,\n"
            "  search with an empty query and a `doc_types` filter.",
        ]

    @staticmethod
    def _guide_bootstrap_section() -> list[str]:
        return [
            "\n## ⚠️ This brain is empty — start here",
            "No doc_types are defined yet, so `put_entity` will reject everything: an\n"
            "entity cannot exist without its concept. Define the concepts first.\n"
            "\n"
            "Ask what domain this brain is for, then model 3–6 concepts and the\n"
            "relationships between them before adding entities. A support brain might be\n"
            "`product`, `issue`, `user_segment`; an infra brain `service`, `datastore`,\n"
            "`runbook`. Create each with `create_doc_type`, then add entities with\n"
            "`put_entity`, always setting `related_to`.",
        ]

    @staticmethod
    def _guide_write_section() -> list[str]:
        return [
            "\n## How to write",
            "\n### Add or update an entity — `put_entity`",
            "```python\n"
            "put_entity(\n"
            '    doc_type="issue",\n'
            '    id="issue:payment-declined",          # must be "<doc_type>:<slug>"\n'
            '    name="Payment declined at checkout",\n'
            '    fields={"severity": "high", "status": "open"},   # schema fields go here\n'
            "    related_to=[\n"
            '        {"target": "product:checkout",\n'
            '         "relationship_edge_meaning": "is a common issue of"},\n'
            "    ],\n"
            ")\n"
            "```\n"
            "- It is an **upsert** — writing the same id again replaces that entity.\n"
            "- `fields` holds the doc_type's schema fields. Do not put `id`, `doc_type`,\n"
            "  `name`, or `related_to` inside it; they are separate arguments.\n"
            "- Prefer a relationship meaning the doc_type already declares or uses (listed\n"
            "  per type below) over inventing a near-duplicate phrasing.\n"
            "- Edges may point at entities that don't exist yet; they resolve later.\n"
            "- Whether a JSON file is written is decided by the doc_type's `storage`\n"
            "  policy, not by you — see below.",
            "\n### Define a new concept — `create_doc_type`",
            "Only when no existing doc_type fits. Check the inventory below first;\n"
            "reusing a type is almost always better than adding a near-duplicate.\n"
            "```python\n"
            "create_doc_type(\n"
            '    doc_type="runbook",\n'
            '    description="A procedure for handling a known failure.",\n'
            '    storage="file",\n'
            "    fields=[\n"
            '        {"name": "name", "type": "string", "search": "syntactic", "boost": 6},\n'
            '        {"name": "steps", "type": "text", "search": "semantic"},\n'
            "    ],\n"
            "    relationships=[\n"
            '        {"name": "resolves", "target_doc_type": "alert"},\n'
            "    ],\n"
            ")\n"
            "```",
            "\n### Schema vocabulary",
            "Field spec keys (only `name` is required):\n"
            "\n"
            "| key | values | meaning |\n"
            "|---|---|---|\n"
            "| `type` | `string` `text` `number` `boolean` `timestamp` | the value's data type |\n"
            "| `processing` | `keyword` `text` `timestamp` | `keyword` = exact/filter, `text` = tokenized |\n"
            "| `search` | `syntactic` `semantic` `none` | `syntactic` = keyword match, `semantic` = vector, `none` = not indexed |\n"
            "| `boost` | number > 0 (default 1) | search weight; a hit in `boost: 6` outranks `boost: 1` 6-to-1 |\n"
            "| `required` | `true` / `false` | reject entities missing this field |\n"
            "\n"
            "Convention: give every type a `name` field with a high boost, and a\n"
            "`description` field with `search: semantic` so it's findable by meaning.\n"
            "\n"
            "`relationships` declares the edge vocabulary for the type — each entry is\n"
            "`{\"name\": \"<meaning>\", \"target_doc_type\": \"<type>\"}`. Declaring makes edges\n"
            "discoverable and lightly validated; entities may still use other meanings.\n"
            "\n"
            "**`storage` — where this type's entities live (choose deliberately):**\n"
            "- `\"index\"` (default) — the search DB owns them; **no files written**. Right\n"
            "  for connector-pulled, high-volume, or temporal data that would churn git.\n"
            "- `\"file\"` — JSON under `entities/<doc_type>/` is the source of truth,\n"
            "  git-tracked and reviewable. Right for curated, human/agent-authored data.",
        ]

    def _guide_doc_types_section(
        self, counts: dict[str, int], examples_per_type: int
    ) -> list[str]:
        if not self.config.doc_types:
            return ["\n## Doc types", "\n_None defined yet._"]

        lines = ["\n## Doc types (what this brain already knows)"]
        for name, dt in self.config.doc_types.items():
            count = counts.get(name, 0)
            noun = "entity" if count == 1 else "entities"
            lines.append(f"\n### {name} ({count} {noun} · storage: {dt.storage})")
            if dt.description:
                lines.append(dt.description)

            if dt.fields:
                # Full detail, not just searchable names: an agent authoring an
                # entity needs the types and which fields are mandatory.
                lines.append("- **Fields:**")
                for f in dt.fields:
                    bits = [f.type, f.search]
                    if f.boost != 1:
                        bits.append(f"boost {f.boost:g}")
                    if f.required:
                        bits.append("**required**")
                    desc = f" — {f.description}" if f.description else ""
                    lines.append(f"  - `{f.name}` ({', '.join(bits)}){desc}")

            if dt.relationships:
                decl = ", ".join(
                    f"`{r.name}`" + (f" → {r.target_doc_type}" if r.target_doc_type else "")
                    for r in dt.relationships
                )
                lines.append(f"- **Relationships (declared):** {decl}")
            observed = self.observed_relationships(name)
            if observed:
                obs = ", ".join(
                    f'"{m}" ×{n}'
                    for m, n in sorted(observed.items(), key=lambda x: -x[1])
                )
                lines.append(f"- **Relationships (in use):** {obs}")

            examples = self.backend.all_entities([name])[:examples_per_type]
            if examples:
                lines.append("- **Examples:** " + ", ".join(f"`{e.id}`" for e in examples))
            lines.append(f'- **Query:** `search_brain(query="...", doc_types=["{name}"])`')
        return lines

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
