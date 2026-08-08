"""The Brain handle — one object the CLI, MCP server, and UI all talk to.

Opening a brain loads its config + doc_type schemas and connects the configured
search backend. `index()` (re)loads entities from disk into the backend.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from time import perf_counter
from typing import Any, Optional

from open_index.config import BrainConfig, iter_entity_files, load_brain_config
from open_index.models import Entity
from open_index.schema import DocType
from open_index.storage import get_backend
from open_index.storage.base import SearchBackend, SearchResults
from open_index.analytics import AnalyticsStore, NullAnalyticsStore


class Brain:
    def __init__(self, config: BrainConfig, backend: Optional[SearchBackend] = None):
        self.config = config
        # Populated by index(): per-entity failures that did not abort the run.
        # Read this after indexing — a silent partial load is the failure mode
        # this list exists to prevent.
        self.index_errors: list[str] = []
        self.backend: SearchBackend = backend or get_backend(config)
        self.backend.ensure_schema(config.doc_types)
        try:
            self.analytics = AnalyticsStore(config.root)
        except (OSError, sqlite3.Error):
            # Analytics are observational; a read-only runtime must still query.
            self.analytics = NullAnalyticsStore()

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
        source: Optional[str] = None,
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
        started = perf_counter()
        try:
            results = self.backend.search(query, doc_types, limit, counts_only,
                                          semantic_weight=semantic_weight)
        except Exception as exc:
            if source:
                self._record_fetch(
                    source=source, operation="search", started=started, query=query,
                    doc_types=doc_types, success=False, error=type(exc).__name__,
                )
            raise
        if counts_only or (min_confidence <= 0 and as_of is None):
            if source:
                self._record_fetch(
                    source=source, operation="search", started=started, query=query,
                    doc_types=doc_types, result_count=results.total,
                    result_doc_types=results.doc_type_counts,
                )
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
        if source:
            self._record_fetch(
                source=source, operation="search", started=started, query=query,
                doc_types=doc_types, result_count=results.total,
                result_doc_types=results.doc_type_counts,
            )
        return results

    def get_entity(
        self, entity_id: str, source: Optional[str] = None
    ) -> Optional[Entity]:
        started = perf_counter()
        try:
            entity = self.backend.get_entity(entity_id)
        except Exception as exc:
            if source:
                self._record_fetch(
                    source=source, operation="get_entity", started=started,
                    entity_id=entity_id, success=False, error=type(exc).__name__,
                )
            raise
        if source:
            self._record_fetch(
                source=source, operation="get_entity", started=started,
                entity_id=entity_id, result_count=int(entity is not None),
                result_doc_types=({entity.doc_type: 1} if entity else {}),
            )
        return entity

    def record_fetch(self, *, started: float, **event: Any) -> None:
        """Keep analytics best-effort so context access remains the priority."""
        try:
            self.analytics.record(duration_ms=(perf_counter() - started) * 1000, **event)
        except Exception:
            pass

    # Internal alias keeps query code compact while the public method also lets
    # alternate UI backends use the same best-effort analytics path.
    _record_fetch = record_fetch

    def analytics_summary(self) -> dict[str, Any]:
        return self.analytics.summary()

    def analytics_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.analytics.recent(limit)

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
        self, examples_per_type: int = 3, source: Optional[str] = None,
        include_writes: bool = True,
    ) -> str:
        """A markdown guide telling an agent how to navigate *this* brain.

        Mirrors the reference `build_navigation_guidelines`: enumerate every
        doc_type with its count, fields (marking searchable ones + boost),
        example entities, and a concrete search call — plus how to write back.
        This is the read tool an agent calls first to orient itself."""
        started = perf_counter()
        counts = self.counts()
        total = sum(counts.values())
        lines: list[str] = []
        lines.append(f"# {self.config.name} — Domain Context Instructions")
        if self.config.description:
            lines.append(f"\n{self.config.description}")
        lines.append(
            "\nOpen Index is the context layer for this domain-specialized agent. "
            "Use this brain to ground domain work in structured context, traverse "
            "related knowledge, and keep durable domain knowledge current.\n\n"
            f"This brain holds **{total} entities** across "
            f"**{len(self.config.doc_types)} doc_types**. Each entity may link to "
            "others via `related_to` edges (`target` + `relationship_edge_meaning`)."
        )

        lines.append("\n## How to read")
        lines.append(
            "- `search_brain(query=\"...\", doc_types=[...], limit=N)` — search/filter entities.\n"
            "- `get_entity(entity_id)` — one entity plus its incoming/outgoing relationships.\n"
            "- Start with a query; filter by `doc_types` to narrow."
        )
        lines.append("\n## Retrieval workflow")
        lines.append(
            "1. Start with `search_brain(query=\"...\")` using the user's domain terms.\n"
            "2. Narrow with `doc_types=[...]` when the concept is known.\n"
            "3. Call `get_entity(entity_id)` for promising results to fetch full fields and edges.\n"
            "4. Follow relationship targets when the answer depends on connected entities; "
            "the edge meaning explains why the traversal is relevant.\n"
            "5. If results are empty, broaden the terms before assuming the index has no context."
        )
        if include_writes:
            lines.append("\n## How to write")
            lines.append(
                "Read and write access is the default. Write back durable, validated "
                "domain knowledge when work reveals something worth retaining.\n"
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

        guide = "\n".join(lines)
        if source:
            self._record_fetch(
                source=source, operation="navigation_guidelines", started=started,
                result_count=total, result_doc_types=counts,
            )
        return guide

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
