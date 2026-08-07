# Entity management

## Entity management model

A **doc type** defines a domain concept, fields, storage policy, display
settings, and optional relationship vocabulary. An **entity** is one typed
instance. Every entity has a deterministic ID in the form
`<doc_type>:<slug>`, a complete current value, and zero or more directed,
labeled outgoing relationships:

```json
{
  "id": "issue:linear-ENG-123",
  "doc_type": "issue",
  "name": "Checkout confirmation is delayed",
  "status": "open",
  "related_to": [
    {
      "target": "product:checkout",
      "relationship_edge_meaning": "affects"
    }
  ]
}
```

`id`, `doc_type`, `name`, and `related_to` are entity keys. Doc-type
declarations drive required-field validation and configured search/indexing
behavior. Validation also checks a declared relationship's target doc type when
the target already exists. Undeclared fields and most declared field value types
are not currently rejected, so this is not comprehensive JSON Schema type
validation.

An entity ID identifies one **current** entity, not an append-only event or a
historical version. Plan the ID namespace before connecting sources or allowing
agents to write it.

## Supported write paths

All supported paths produce a complete entity and use the same write behavior:

| Path | Use | Persistence behavior |
| --- | --- | --- |
| Entity JSON files plus `droid-brain index` | Curated file-backed entities or bulk edits | `index` reloads file-backed doc types from JSON files. |
| `droid-brain add-entity <file>` | Validate and write one JSON file or JSON array | Uses the doc type's storage policy. |
| Python `Brain.put_entity(entity)` | Application or controlled agent workflow | Uses the doc type's storage policy. |
| MCP `put_entity` | An MCP client writes a complete entity | Uses the doc type's storage policy. HTTP exposes MCP tools, not generic upload or REST entity resources. |
| Connectors | Trusted local Python extraction code | After `EntitySpec.to_entity()` succeeds, `Brain.put_entity` failures are reported for that entity while later entities continue. Construction errors occur before that per-write handling and can abort the run. |

The UI is read-only. Its **Edit** tab provides guidance rather than a form or
upload path. Connector scheduling is interval/due checking, with due-state in
`.droid_brain_state.json`; it is not a cron-expression parser. Use an external
scheduler when one is needed.

## Choose the source of truth

Storage is selected per doc type. It controls Droid Brain's authority for the
current value; an upstream system may remain the business authority in either
model.

| Storage | Droid Brain authority | Normal writers | Persistence | Version and history behavior | `index` behavior | Recovery | Best fit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `file` | JSON under `entities/<doc_type>/` is the current authority. | Humans, agents, CLI/MCP, or controlled generation writing complete JSON. | CLI/MCP/Python writes persist complete JSON and update the backend. | JSON is Git-versionable, but review, history, and rollback require an operator to commit revisions. Droid Brain does not commit for you. | Deletes/reloads that doc type's backend rows from files, reconciling file edits and removals. | Restore a committed revision or separate backup, then run `index`. | Small, reviewable, curated current records. |
| `index` | The configured SQLite or OpenSearch backend is the current authority. | Connectors, agents, CLI/MCP, or Python writers. | Current entities live in the backend; no entity JSON is written. | There is no entity JSON or Git history. A same-ID write replaces the previous current value. | Leaves the doc type untouched because there are no authoritative entity files. | Restore a database/backend backup, repeat ingestion, or replay an external immutable snapshot/manifest. | Generated, high-volume, or frequently refreshed current records. |

Do not rely on a file-backed backend row as history: it is a materialized copy of
the current JSON. An index-backed entity is current state only unless you retain
historical artifacts elsewhere.

## Stable IDs, retries, and writer ownership

Use a stable source-derived ID when one source record represents one current
entity. For example, `issue:linear-ENG-123` can be owned by the connector that
maps upstream issue `ENG-123` to the `issue` doc type.

A retry is **idempotent only when it resends the same complete entity**,
including all fields and `related_to` entries. A partial retry or a recomputed
payload can replace the current state differently.

Assign one writer to each ID namespace, such as `issue:linear-*`, or serialize
writes with an external queue, lock, or transactional system. Droid Brain does
not provide per-entity write ownership, version preconditions, or optimistic
concurrency controls.

## Replacement and concurrency

`put_entity` is replacement by ID, not a patch or merge. The replacement must
contain the complete desired current entity. Fields left out of the new payload
and the replaced entity's old outgoing relationships are removed. Incoming
relationships stored on other entities remain.

First write this current entity:

```json
{
  "id": "issue:linear-ENG-123",
  "doc_type": "issue",
  "name": "Checkout confirmation is delayed",
  "status": "open",
  "severity": "high",
  "related_to": [
    {
      "target": "product:checkout",
      "relationship_edge_meaning": "affects"
    }
  ]
}
```

Another entity independently points to it:

```json
{
  "id": "comment:release-note-7",
  "doc_type": "comment",
  "name": "Release note follow-up",
  "related_to": [
    {
      "target": "issue:linear-ENG-123",
      "relationship_edge_meaning": "tracks"
    }
  ]
}
```

Then write this smaller, complete replacement for `issue:linear-ENG-123`:

```json
{
  "id": "issue:linear-ENG-123",
  "doc_type": "issue",
  "name": "Checkout confirmation is delayed",
  "status": "resolved"
}
```

Afterward, `severity` is gone and the issue's `affects` edge is gone. The
comment's incoming `tracks` edge remains because it is owned by
`comment:release-note-7`.

Read → construct the complete desired entity → write is a useful completeness
pattern, but it is not concurrency control. Two writers can read the same prior
value, construct different replacements, and race; the last write wins and can
discard the other writer's intervening update. Use the ownership or external
serialization pattern above for multi-writer workloads.

## Source provenance convention

Droid Brain has no reserved or automatically populated provenance fields. When
an entity represents an ordinary source record, define user-owned fields such
as the following and populate them in the complete write:

```yaml
schema:
  fields:
    - { name: source_system, type: string, processing: keyword, search: none }
    - { name: source_record_id, type: string, processing: keyword, search: none }
    - { name: source_revision, type: string, processing: keyword, search: none }
    - { name: source_content_hash, type: string, processing: keyword, search: none }
    - { name: source_uri, type: string, processing: keyword, search: none }
    - { name: observed_at, type: timestamp, processing: timestamp, search: none }
```

These names are a convention, not a built-in schema or lineage API. `search:
none` is appropriate for metadata that should not contribute to retrieval; it
does not make the field private or enforce access control. Choose immutable
source revisions and content hashes where the upstream system provides them.

## AI-distillation run-manifest convention

For an AI-distillation workflow, keep one immutable external manifest per run.
Droid Brain neither generates nor validates this manifest, and a same-ID
replacement destroys the prior current value. This synthetic JSON shows a
complete convention-only manifest:

```json
{
  "run_id": "distill-2026-08-07T12:00:00Z-7f3d",
  "run_at_utc": "2026-08-07T12:00:00Z",
  "source_artifacts": [
    {
      "source_id": "handbook:payments/incident-guide",
      "source_revision": "git:7f8e9d0",
      "source_uri": "https://docs.example.invalid/handbook/payments/incident-guide",
      "content_hash": "sha256:9a2b1c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef"
    }
  ],
  "extractor": {
    "repository": "https://code.example.invalid/agents/support-distiller",
    "commit": "a1b2c3d4e5f6"
  },
  "prompt": {
    "template_name": "support-claim-extractor",
    "immutable_version": "v3",
    "template_hash": "sha256:6f5e4d3c2b1a0987654321fedcba9876543210abcdef1234567890fedcba9876"
  },
  "generation": {
    "provider": "example-llm-provider",
    "model": "example-model-2026-06",
    "temperature": 0.0,
    "top_p": 1.0,
    "seed": 42,
    "max_tokens": 1800,
    "structured_output_schema_version": "claim-v2"
  },
  "outputs": [
    {
      "entity_id": "claim:payments-incident-guide-7f3d",
      "content_hash": "sha256:11223344556677889900aabbccddeeff00112233445566778899aabbccddeeff"
    }
  ],
  "retained_artifacts": {
    "prior_entity_versions": "object://knowledge-history/distill-2026-08-07T12:00:00Z-7f3d/entities/",
    "source_artifacts": "object://knowledge-history/distill-2026-08-07T12:00:00Z-7f3d/sources/",
    "store_snapshot": "object://knowledge-history/distill-2026-08-07T12:00:00Z-7f3d/store-snapshot/"
  }
}
```

Record immutable source IDs, revisions, applicable URIs, and content hashes;
the extractor repository and code commit; prompt/template name and immutable
version or hash; model provider/name and generation parameters (temperature,
top-p, seed when supported, max tokens, and structured-output/schema version);
run ID and UTC time; output entity IDs/hashes; and locations for retained prior
entity versions, source artifacts, and store snapshots.

For small reviewable output, use `storage: file`, commit entity and manifest
revisions, and record the Git commit. For larger output, use `storage: index`
for current state plus an external append-only manifest/object store and a
SQLite/OpenSearch snapshot and source snapshot sufficient to replay the run.
Neither strategy is supplied automatically by Droid Brain.

## Conflicting claims convention

Represent each source assertion as a separate `claim` entity and link it to the
subject it asserts something about. Preserve source/provenance fields and, when
useful, link the claim to a run manifest retained outside the brain. For example,
`claim:payments-policy-2026-08-07-a` and
`claim:payments-policy-2026-08-07-b` may both point to
`policy:refund-window` with an `asserts_about` relationship.

Droid Brain does not detect contradictions, adjudicate conflicting assertions,
or automatically rank one claim as true. A caller or external workflow must
decide how to present, reconcile, or supersede them.

## Periodically refreshed and upserted records

Connector output and other recurring ingestion are **periodically
refreshed/upserted current records**, not snapshots:

- Each run replaces only the IDs it emits.
- An upstream ID omitted from a later response remains persisted and may become
  stale; non-emission is neither deletion nor resolution.
- Querying a recent source window limits extraction. It does not remove older
  IDs that were already persisted.
- Freshness requires the source/connector to emit an explicit `resolved`,
  `inactive`, or `deleted` state, or an external full reconciliation, deletion,
  or rebuild of the current-state store.
- A repeated same-ID record is current state, not history.
- True historical snapshots require run-scoped entities and IDs, for example
  `deployment_snapshot:run-2026-08-07:source-42`, with explicit relationships
  to the stable subject such as `deployment:source-42`.

If the upstream system cannot emit inactive state, define and operate an
external reconciliation policy before relying on records for decisions that
need currentness.

## Reproducibility and recovery checklist

For every important ingestion or distillation run, retain enough material to
rebuild the intended current state:

1. Record the doc-type schema and connector/extractor Git commits.
2. Retain immutable source and run manifests, including source revisions and
   content hashes.
3. Retain either the committed file-backed entity revision or a backend/store
   backup or snapshot for index-backed state.
4. Record the embedding provider, model, and dimension when embeddings are in
   use, plus model/configuration changes that require re-embedding.
5. Document the replay order: restore source artifacts, restore or recreate the
   schema, rerun deterministic ingestion, restore/rebuild the store, then
   re-embed or re-index as appropriate.
6. Test recovery against a disposable brain before treating artifacts as a
   recovery procedure.

This checklist is an operator convention. It does not turn current-state storage
into built-in version history or make nondeterministic extraction fully
reproducible.

## Authentication, authorization, and trust boundaries

**Authentication** establishes who or what is calling. **Authorization**
decides which actions and resources that authenticated principal may access.
They are different controls. Current Droid Brain deployments provide limited
authentication/tool-mode controls, not policy-aware authorization.

### Current transport and endpoint controls

- HTTP MCP can use one optional static bearer token. When configured, that same
  token is checked uniformly on every HTTP request. It proves possession of the
  shared secret; it does not establish a user/service principal, claims, or
  resource-specific permissions.
- Stdio has no Droid Brain authentication layer. Its trust boundary is the
  local process/user and access to the brain directory and backend credentials.
- `--read-only` removes both `put_entity` and `create_doc_type` for the entire
  MCP server. A separate read-only endpoint can use a separate token from a
  read-write endpoint, but this is coarse endpoint mode, not authorization of
  individual principals or resources.
- HTTP serves streamable MCP, not a generic REST entity API.

### Controls that are not present

Droid Brain has no principal/claims model; tenant or workspace isolation;
per-brain, per-doc-type, per-entity, per-field, per-relationship, or per-source
ACLs; RBAC/ABAC; policy engine; row-level filtering; write ownership; or audit
trail. It does not apply a common deny-by-default policy to exact get,
keyword/vector/hybrid search, counts, navigation guidance, relationships or
traversal, schema tools, connector writes, embeddings/indexes, exports, or
backups.

`tenant_id`, `visibility`, and ACL-like entity fields are ordinary data. They
do not protect any of those paths. Caller-supplied `doc_types` is query scoping,
not authorization, and an agent prompt that instructs a caller to stay within a
boundary is advisory rather than a security control. OpenSearch credentials
secure direct backend connectivity; they do not authorize application callers
through Droid Brain.

### Current pattern for separate trust boundaries

For customer/workspace or public/internal/restricted boundaries, deploy each
boundary separately:

1. Use a distinct brain directory and distinct SQLite database or OpenSearch
   backend/index per boundary.
2. Run separate MCP processes and endpoints with separate static tokens, such
   as `${DROID_BRAIN_BOUNDARY_A_READ_TOKEN}` and
   `${DROID_BRAIN_BOUNDARY_A_WRITE_TOKEN}`, and least-privileged OS, container,
   and backend identities.
3. When readers and writers differ, use distinct read-only and read-write
   endpoints/tokens rather than sharing a writable token.
4. Isolate connector credentials and destinations, backups, exports, and
   embedding/index artifacts by the same boundary. Supply credentials through
   environment variables such as `${SOURCE_SYSTEM_TOKEN}`, never committed
   values.
5. Put external TLS and authentication gateway controls, network restrictions,
   token storage/rotation, and durable request/audit logging around endpoints as
   required by the deployment.

This is coarse physical/deployment segregation. It is not multi-tenant policy
enforcement, RBAC/ABAC, or authorization inside a single brain. Do not combine
trust boundaries in one brain and rely on prompts, `doc_types`, name prefixes,
policy-like fields, or backend credentials to prevent leakage.
