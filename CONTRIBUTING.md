# Contributing

## Ways to contribute

Help make Droid Brain more useful for real agent workflows: model a domain
brain, build an offline-safe connector, improve retrieval, refine the explorer
UI, or contribute a reproducible evaluation. Code, examples, documentation, and
small reproducible bug or behavior reports are all welcome. Use synthetic data
rather than customer or production data.

## Development and CI setup

Use the editable all-extras install for contributor work and CI. End-user
recipes should install only the extras they need.

```bash
git clone <repository-url>
cd droid-brain
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[all]'
```

`.[all]` is reserved for this contributor/CI path.

## Repository map

| Area | Location |
| --- | --- |
| Core brain model and CLI | `droid_brain/brain.py`, `droid_brain/models.py`, `droid_brain/schema.py`, `droid_brain/cli.py` |
| Storage backends | `droid_brain/storage/` |
| MCP tools and HTTP serving | `droid_brain/mcp_server.py` |
| Connectors and scheduling | `droid_brain/connectors/`, `droid_brain/scheduling.py` |
| Read-only explorer UI | `droid_brain/ui/` |
| Runnable example brains | `examples/` |
| Test suite | `tests/` |
| Documentation | `README.md`, `entity-management.md`, `cookbooks.md`, `ROADMAP.md`, and this file |

## Validation matrix

Run the applicable checks before proposing a change.

| Change or capability | Command or probe | Expected scope |
| --- | --- | --- |
| Full suite | `python -m pytest -q` | Current unit and integration tests; live OpenSearch tests skip unless configured. |
| Bundled examples | `for brain in examples/*-brain; do droid-brain validate --brain "$brain"; done` | Validates every bundled brain’s YAML and JSON. |
| Complete-replacement behavior | Use a disposable temporary brain with one file-backed and one index-backed doc type. For each, write a complete entity with a field and outgoing edge, add another entity with an incoming edge, then replace the original ID with a smaller complete payload. Verify the omitted field and old outgoing edge are gone, the incoming edge remains, file-backed JSON matches the replacement, and index-backed storage writes no JSON. | Run on SQLite; do not commit the temporary brain or database. |
| Live OpenSearch | `OPENSEARCH_URL="${OPENSEARCH_URL:?must be set}" python -m pytest tests/test_opensearch_integration.py -q` | Optional. Requires a disposable OpenSearch target. Fixture cleanup clears documents from its unique test index but leaves the index itself. |

The live test module is `tests/test_opensearch_integration.py`; it is skipped
when run directly without `OPENSEARCH_URL`. Use the fail-fast command above so
the optional validation cannot silently skip. Do not present a skipped optional
integration test as live-backend coverage.

## Examples and connectors

Examples and connectors should be deterministic, complete, offline-safe by
default, and covered by tests. Use synthetic records such as `issue:demo-123`,
not real organization data.

- Give each writer ownership of one ID namespace, or document the external
  serialization that prevents competing writers.
- Emit complete entity payloads. A same-ID retry is idempotent only when it
  repeats the same complete payload.
- State an explicit stale-record policy: emit a resolved/inactive state,
  reconcile externally, or document why records remain current-state data.
  Omission from a connector response does not delete an existing entity.
- Obtain credentials and endpoints from environment variables, for example
  `${SOURCE_API_TOKEN}` and `${SOURCE_API_URL}`. Do not commit secrets,
  databases, generated state, or production endpoints.
- Keep the default example path runnable without network access. Make any live
  service dependency optional and test the offline behavior.

## Documentation accuracy

Accurate documentation helps readers choose the right installation, storage,
retrieval, and security approach before they invest in an integration. Describe
the checked-out implementation and its tests, not a desired design.

- Recheck source installation instructions and the PyPI package state before
  claiming a published install path. Use the correct optional extras and reserve
  `.[all]` for contributor/CI setup.
- Say file-backed entities are Git-versionable, not automatically committed.
  Current state, history, review, and rollback are different concerns:
  history and rollback require operator-retained commits or external snapshots.
- Describe same-ID writes as complete replacement: omitted fields and outgoing
  relationships disappear; incoming relationships owned by other entities
  remain. Read-construct-write can avoid accidental omissions but does not make
  concurrent writes safe; last-write-wins can lose updates.
- Label source provenance fields and AI-distillation manifests as conventions
  unless the implementation builds and validates them.
- State that normal connector refreshes update emitted records only. Source
  omission, source windows, and retries do not provide deletion, reconciliation,
  or history automatically.
- Preserve exact search limits: SQLite keyword search reranks at most 500 FTS
  candidates, while SQLite semantic and hybrid search scan at most 10,000
  name-sorted entities in scope. OpenSearch all-entity and relationship scans
  have a 10,000-result cap. These are code caps, not performance guarantees.
- State that OpenSearch k-NN semantic search ships today. SQLite-native vector
  indexing is future roadmap work, not a current feature.

## Authentication and authorization contribution expectations

### Current controls and limits

Current HTTP bearer-token and server-wide `--read-only` modes are coarse
transport/tool controls, not first-class caller authorization. For the complete
current boundary—including local stdio, absent principals and resource policy,
and deployment segregation—see [Authentication, authorization, and trust
boundaries](./entity-management.md#authentication-authorization-and-trust-boundaries).

### Future authorization test matrix

Once first-class authorization exists, contributions must include allow, deny,
and adversarial no-leak coverage for:

- exact `get_entity` access;
- keyword, vector, and hybrid search filtering before ranking;
- total and per-doc-type counts and aggregations;
- navigation guidance and examples;
- incoming and outgoing relationships and graph traversal;
- entity writes plus doc-type and schema tools;
- connector/service identities and scoped writes; and
- SQLite/OpenSearch parity, embeddings and reindexing, exports, backups, and
  migrations where applicable.

This is a roadmap acceptance matrix, not a description of authorization code
or allow/deny/no-leak tests currently present in this repository.

## Change checklist

- [ ] Add or update focused tests for changed behavior, and run the relevant
      validation matrix entries.
- [ ] Verify documentation links, commands, and snippets against the checked-out
      code.
- [ ] Keep examples synthetic; do not commit databases, generated state, or
      secrets. Use `${ENV_VAR}` placeholders for configuration values.
- [ ] Align documentation, example metadata, schemas, and connector behavior.
