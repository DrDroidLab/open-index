# Droid Brain

**An open context graph and memory layer for AI agents.** Droid Brain gives an
agent a typed entity model, explicit relationships, deterministic lookup, and
MCP read/write tools over an inspectable local brain.

[Quickstart](#quickstart-from-source) · [Why it fits](#where-it-fits) · [Status](#what-works-today) · [Cookbooks](./cookbooks.md) · [Entity lifecycle](./entity-management.md) · [Roadmap](./ROADMAP.md)

## Why domain-specialized agents need a context layer

An agent operating in a support, infrastructure, or business domain needs more
than transient prompt context. It needs stable concepts, relationships across
sources, predictable lookup, controlled write-back of current state, and
persistence that an operator can inspect. Droid Brain models those concepts as
typed entities and directed, labeled relationships. It is not a general-purpose
policy engine, historical event store, or autonomous knowledge-improvement
system.

## Where it fits

Droid Brain composes with files and agent skills, and with chunk-based RAG; it
does not replace them. Files and skills carry instructions and source material.
Chunk RAG retrieves unstructured passages. Droid Brain stores modeled current
facts and relationships when an agent benefits from entity identity, a schema,
relationship traversal, or deterministic exact lookup. Use the combination that
matches the task.

## What works today

| Area | Available now | Important current limit |
| --- | --- | --- |
| Model and validation | Typed doc types, fields, entities, declared relationship vocabulary, and required-field checks | Validation is limited; it is not comprehensive JSON Schema type validation. Declared edges are checked for target type only when the target already exists; undeclared meanings and forward references are allowed. |
| Relationships | Directed `related_to` edges, relationship-aware exact reads, navigation guidance, and read-only map exploration | Relationships are data, not access-control boundaries. |
| Search | SQLite FTS5 keyword search, field boosts, optional semantic/hybrid search, and OpenSearch fuzzy search | See [Search and embeddings](#search-and-embeddings) for provider requirements and exact caps. |
| OpenSearch | Optional backend with field boosts and k-NN semantic search | Requires a separately configured backend; it does not add caller authorization. |
| Agent surfaces | MCP over local stdio; optional HTTP MCP; server-wide read-only mode | MCP has five tools; HTTP token authentication is an endpoint gate, not resource authorization. |
| Connectors and UI | Trusted local Python connectors, due/interval checks, per-entity write-error reporting, and a read-only UI | Entity-construction or extraction failures can abort a run; only write failures after successful construction are isolated per entity. Scheduling is not cron parsing; use an external scheduler where required. The UI has no edit/upload form. |
| State and history | Per-type file or index current-state storage | File records are Git-versionable only when an operator commits them. Same-ID writes replace current state; provenance and run manifests are conventions. |
| Not available | Policy-aware authorization, tenant isolation, temporal lifecycle controls, type-level boosts, SQLite-native vector indexing, and published benchmarks | These are current limits or [roadmap](./ROADMAP.md) items, not implied capabilities. |

## Quickstart from source

The supported acquisition path is a source checkout.

```bash
git clone https://github.com/DrDroidLab/droid-brain.git
cd droid-brain
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .

droid-brain index --brain examples/support-brain
droid-brain validate --brain examples/support-brain
droid-brain search "payment declined" --brain examples/support-brain --doc-type issue
```

The base install supports keyword search. The support example declares semantic
fields, but without an embedding provider Droid Brain logs a warning and falls
back to keyword search rather than requiring `fastembed`.

## Add optional surfaces

Install an extra immediately before using its surface:

```bash
# Read-only map explorer
python -m pip install '.[ui]'
droid-brain ui --brain examples/support-brain

# MCP over local stdio
python -m pip install '.[mcp]'
droid-brain mcp --brain examples/support-brain

# Local semantic embeddings and hybrid retrieval
python -m pip install '.[semantic]'
droid-brain index --brain examples/support-brain --reembed
```

## Five-minute support-agent walkthrough

The bundled support brain contains queryable `product`, `issue`, `user_segment`,
and `comment` entities. Its files are curated current state; index them first:

```bash
droid-brain index --brain examples/support-brain
droid-brain search "checkout" --brain examples/support-brain
```

For a local MCP client, create client configuration yourself. The bundled
example deliberately does not contain `.mcp.json`; do not add this generated
configuration to it merely to use the example.

```bash
test ! -e examples/support-brain/.mcp.json
cat > .mcp.json <<'JSON'
{
  "mcpServers": {
    "support-brain": {
      "command": "droid-brain",
      "args": ["mcp", "--brain", "examples/support-brain"]
    }
  }
}
JSON
```

`droid-brain init my-brain` generates `.mcp.json` only for a newly scaffolded
brain; it does not create configuration for existing examples. After the MCP
client loads this configuration, start with `navigation_guidelines`, then
search and inspect entities. For example:

> Call `navigation_guidelines` first. Then investigate a customer reporting a
> declined payment at checkout: search the relevant issues, inspect the best
> candidate and its relationships, and identify related product, segment, or
> comment context before answering.

This is a retrieval and context workflow, not a guarantee that the result is
complete or correct for a live customer case.

## Core model

A **doc type** is a named domain concept with a schema, storage policy, display
settings, and optional relationship vocabulary. An **entity** is one typed,
current instance with an ID of the form `<doc_type>:<slug>`. A **relationship**
is an outgoing `related_to` record containing a target and free-text meaning. A
**connector** is trusted local Python code that emits complete entities through
the same validation/write path.

```yaml
# doc_types/service.yaml
doc_type: service
description: A deployed service.
storage: file
display:
  label_field: name
  color: "#7c3aed"
schema:
  fields:
    - { name: name, type: string, processing: keyword, search: syntactic, boost: 6, required: true }
    - { name: description, type: text, processing: text, search: semantic }
    - { name: owner, type: string, processing: keyword, search: syntactic }
relationships:
  - { name: "writes to", target_doc_type: datastore }
```

```json
{
  "doc_type": "service",
  "id": "service:checkout",
  "name": "Checkout",
  "owner": "payments-team",
  "related_to": [
    {
      "target": "datastore:postgres-main",
      "relationship_edge_meaning": "writes to"
    }
  ]
}
```

A field's `search` is `syntactic`, `semantic`, or `none`; `boost` is a positive
relative field weight. Required field presence is validated. A declared
relationship can be lightly checked against an already stored target's type;
that is not a complete schema or graph-integrity system.

## Where current data lives

Choose storage per doc type; this controls Droid Brain's current-state source of
truth, not necessarily the upstream business authority.

| Storage | Current source of truth | `index` behavior | History and recovery |
| --- | --- | --- | --- |
| `file` | JSON in `entities/<doc_type>/` | Replaces backend rows for the file-backed type from current files, including file edits/removals | JSON is Git-versionable and operator-committed; review, history, and rollback require retained commits or other backups. |
| `index` | Configured SQLite or OpenSearch backend | Leaves the type untouched; no entity JSON is written | Same-ID records are current state only. Restore a backend backup, re-ingest, or replay externally retained artifacts. |

File-backed backend rows are materialized copies of current JSON, not history.
For source provenance, AI-distillation run manifests, conflicting claims, and
current-state practices, see [entity management](./entity-management.md).
Those fields and manifests are user conventions; Droid Brain does not create or
validate them.

## Complete writes and writer ownership

A same-ID `put_entity` is replacement, not patch or merge. Make an idempotent
retry by sending the same **complete** entity, including every desired field and
outgoing `related_to` edge. Fields omitted by a replacement and outgoing edges
omitted by that entity disappear; incoming edges owned by other entities remain.

Read → construct the complete desired entity → write prevents accidental
omissions, but it is not concurrency control. Two writers can race after the
read and the last write wins, losing the other writer's update. Assign one
writer to an ID namespace or use external serialization, a queue, lock, or
transactional system. Droid Brain has no version preconditions or write
ownership control.

## Periodically refreshed and upserted records

Connector and recurring ingestion output is **periodically refreshed/upserted
current state**, not snapshots. Each run replaces only IDs it emits. An upstream
record omitted from a later response remains stored and can become stale; a
source-window query does not delete older records.

Have the source emit `resolved`, `inactive`, or `deleted` when appropriate, or
use external full reconciliation, deletion, or rebuild. For true history, emit
run-scoped entity IDs and relate them to a stable subject. See the [lifecycle
and refresh guidance](./entity-management.md#periodically-refreshed-and-upserted-records).

## Use from agents and ingest connectors

MCP exposes five tools:

- `navigation_guidelines()` to orient an agent to doc types, fields, examples, and relationships;
- `search_brain(query, doc_types, limit)` and `get_entity(id)` to read;
- `put_entity(...)` and `create_doc_type(...)` to write when the endpoint is not read-only.

`droid-brain mcp --brain <path>` uses stdio and trusts the local process/user.
`droid-brain serve` exposes the same tools over HTTP; `--read-only` removes both
write tools for that entire endpoint. Connectors are trusted local Python
extraction code. `droid-brain ingest <connector>` runs one; `droid-brain run`
checks whether configured connectors are due. After an entity is constructed,
its `Brain.put_entity` validation/write failure is reported and later entities
continue. Extraction errors or malformed specs that fail during
`EntitySpec.to_entity()` can abort the run. Use an external scheduler for
cron-like orchestration.

## Search and embeddings

SQLite FTS5 provides base keyword search. If a queried scope includes semantic
fields but no provider is available, Droid Brain warns and uses keyword search.
Install local embeddings with:

```bash
python -m pip install '.[semantic]'
```

The local default is `BAAI/bge-small-en-v1.5` (384 dimensions); set
`search.embedding_model` to choose another supported local model. SQLite
semantic/hybrid retrieval requires `.[semantic]` for both local and remote
providers because its vector-scoring path uses NumPy. Then configure an
OpenAI-compatible embedding endpoint with all four variables:

```bash
export DROID_BRAIN_EMBEDDING_BASE_URL="https://embeddings.example.invalid/v1"
export DROID_BRAIN_EMBEDDING_API_KEY="${EMBEDDING_API_KEY}"
export DROID_BRAIN_EMBEDDING_MODEL="embedding-model-name"
export DROID_BRAIN_EMBEDDING_DIM="384"
```

After changing provider, model, or dimensions, re-embed/rebuild the index with
`droid-brain index --brain <path> --reembed`; recreating an OpenSearch index may
also be needed for an incompatible vector dimension. `search.semantic_weight`
controls keyword/semantic blending (default `0.3`; `0` is keyword-only and `1`
is semantic-only).

Exact implementation caps are not benchmark claims: SQLite keyword search
reranks at most **500** FTS candidates. SQLite semantic and hybrid search scan
at most **10,000** name-sorted entities in scope and use a bounded brute-force
vector scan, not SQLite-native vectors. OpenSearch supports fuzzy keyword and
k-NN semantic search; its all-entity and relationship scans cap at **10,000**
results. Measure the backend and workload you operate.

## Authentication is not authorization

> **Important: current authentication is not policy-aware authorization.** Do
> not use a single Droid Brain instance as a multi-tenant or per-caller policy
> boundary.

HTTP accepts one optional static bearer token, checked uniformly on every HTTP
request. Stdio trusts the local process/user. `--read-only` removes both write
tools for a whole endpoint; it does not establish identities, claims, or
resource-specific permissions.

Caller-selected `doc_types` only scopes a query. Prompts, `tenant_id`,
`visibility`, and ACL-like fields are ordinary data and do not enforce access.
OpenSearch credentials protect backend connectivity, not Droid Brain caller
authorization. Current Droid Brain has no principal or claims model,
tenant/workspace boundary, ACL, RBAC/ABAC, policy filtering, write ownership,
relationship-leak prevention, or authorization audit capability.

A real policy would need one consistently enforced decision before exact gets;
keyword, vector, and hybrid ranking; counts and navigation; relationship
traversal; connector writes; doc types and schemas; embeddings and indexes;
exports; and backups. Those controls are not present today; see the
[roadmap](./ROADMAP.md#policy-aware-authorization-and-data-segregation).

## Current trust-boundary deployment pattern

For a local agent, use the stdio process boundary. HTTP `serve` binds to
`0.0.0.0` by default and is writable by default, so set a token and put it
behind your own network/TLS controls before any network use.

For each trust boundary, use a separate brain directory, SQLite database or
OpenSearch backend/index, process, endpoint/port, and token. Separate
read-only and read/write endpoints and tokens; use least-privileged process and
backend identities. An external gateway or network layer can provide TLS,
network restrictions, token rotation, and request auditing; back up each
boundary separately.

Install HTTP serving only when using it, and install OpenSearch only when
configuring that backend:

```bash
python -m pip install '.[serve]'
# only for a configured OpenSearch brain:
python -m pip install '.[opensearch]'

# Bind locally for an external gateway. Required-variable expansion prevents
# an unset token from silently starting an unauthenticated endpoint.
DROID_BRAIN_TOKEN="${WORKSPACE_A_READ_TOKEN:?must be set}" \
  droid-brain serve --brain /srv/droid-brain/workspace-a --host 127.0.0.1 --port 8101 --read-only
DROID_BRAIN_TOKEN="${WORKSPACE_A_WRITE_TOKEN:?must be set}" \
  droid-brain serve --brain /srv/droid-brain/workspace-a --host 127.0.0.1 --port 8102
DROID_BRAIN_TOKEN="${WORKSPACE_B_READ_TOKEN:?must be set}" \
  droid-brain serve --brain /srv/droid-brain/workspace-b --host 127.0.0.1 --port 8201 --read-only
```

This is coarse physical/deployment segregation, **not** in-brain authorization
or multi-tenant policy enforcement. Do not route distinct trust boundaries to
the same brain or backend/index and treat caller-controlled metadata as a
security boundary.

## Benchmarks and known limits

Tests cover current behavior, but there is no standardized published
quality, latency, throughput, or capacity benchmark. The 500/10,000 search
caps above are code caps, not performance or recall guarantees. For unshipped
lifecycle, policy, vector-indexing, provenance, conflict, and benchmark work,
see the [roadmap](./ROADMAP.md).

## Examples, documentation, and project links

- [Support example](./examples/support-brain): products, issues, user segments, and comments.
- [Infrastructure example](./examples/infra-brain): services, datastores, dashboards, runbooks, and periodically refreshed alerts.
- [Personal example](./examples/personal-brain): areas, goals, projects, people, and notes.
- [Cloud example](./examples/cloud-brain): Kubernetes-style infrastructure entities populated by a connector.
- [Cookbooks](./cookbooks.md) for runnable patterns and limits.
- [Entity management](./entity-management.md) for source-of-truth, replacement, provenance, and refresh semantics.
- [Roadmap](./ROADMAP.md) for future work only.
- [Contributing](./CONTRIBUTING.md) for contributor/CI setup and validation.
- [MIT License](./LICENSE).
