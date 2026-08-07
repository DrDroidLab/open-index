# Droid Brain

**An open context graph and memory layer for AI agents.** Droid Brain helps
AI engineers give domain agents modeled organizational knowledge: typed entities,
meaningful relationships, deterministic and semantic retrieval, and MCP tools to
read and update an inspectable brain.

[Quickstart](#quickstart-from-source) · [Agent walkthrough](#five-minute-support-agent-walkthrough) · [Capabilities](#what-you-can-build) · [Cookbooks](./cookbooks.md) · [Entity management](./entity-management.md) · [Roadmap](./ROADMAP.md)

## Give domain agents context they can work with

Domain agents become more context-aware and consistent when their important
concepts have stable identities, useful relationships, and a place to update
current knowledge. Droid Brain is designed to help with that: model products,
services, issues, customers, policies, or any domain as typed entities; connect
them with explicit edges; retrieve them predictably; and make the result
available to an agent over MCP.

The result is an inspectable, updateable context layer that helps agents carry
more of a domain workflow forward with grounded, structured context. It supports
current-state write-back through the same validated path used by files and
connectors, without claiming to resolve conflicting information or improve
knowledge automatically.

## Where it fits

Droid Brain complements the tools you already use. Files and skills remain a
great home for instructions and source material. Chunk-based RAG remains useful
for retrieving passages. Droid Brain adds a modeled layer when an agent needs
stable entity identity, cross-source relationships, exact lookup, or controlled
updates to current domain knowledge. Together, they make it easier to move from
scattered context to a navigable domain model.

## Quickstart from source

Start from a source checkout and explore the bundled support brain:

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

The base install provides SQLite FTS5 keyword search. The support example also
declares semantic fields; without an embedding provider, Droid Brain warns and
uses keyword search so the quickstart stays useful without `fastembed`.

## Five-minute support-agent walkthrough

The bundled support brain models queryable `product`, `issue`, `user_segment`,
and `comment` entities. Index it, then let an agent navigate the model before
answering a support question.

```bash
# Install MCP immediately before using a local MCP client.
python -m pip install '.[mcp]'

droid-brain index --brain examples/support-brain
droid-brain mcp --brain examples/support-brain
```

Create the MCP configuration in your own client workspace. The bundled example
deliberately has no `.mcp.json`:

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

`droid-brain init my-brain` creates `.mcp.json` for a newly scaffolded brain;
it does not add one to an existing example. Once connected, give your agent a
prompt such as:

> Start with `navigation_guidelines`. A customer reports a declined payment at
> checkout. Find the relevant issues, inspect the strongest candidate and its
> relationships, then use the related product, segment, and comments to prepare
> a helpful response.

This workflow gives an agent structured leads and connected context to inspect;
review the retrieved entities before acting on a live case.

## What you can build

- **A domain model agents can navigate.** Define doc types, schema fields, and
  relationship vocabulary for support, infrastructure, operations, personal, or
  other domains. Entities have stable IDs and directed, labeled relationships.
- **Grounded retrieval for agent workflows.** Use deterministic keyword search
  and field boosts with SQLite FTS5; add semantic/hybrid retrieval when needed.
  OpenSearch adds fuzzy and k-NN semantic search for a configured backend.
- **An MCP-native context surface.** Agents can call `navigation_guidelines`,
  `search_brain`, and `get_entity`, then use `put_entity` and
  `create_doc_type` when writes are enabled. Local stdio and optional HTTP MCP
  use the same model.
- **Inspectable, updateable knowledge.** Choose `storage: file` for JSON that
  can be Git-versioned and operator-committed, or `storage: index` for current
  records owned by SQLite or OpenSearch. The read-only UI provides a map and
  search explorer.
- **Connectors for current domain data.** Trusted local Python connectors emit
  complete entities through the normal write path; run them directly or on an
  external schedule.

## Build the model

A **doc type** is a domain concept with fields, storage policy, and optional
relationship vocabulary. An **entity** is one current instance with an ID such
as `service:checkout`. A **relationship** is an outgoing `related_to` edge. A
**connector** is trusted local Python code that produces complete entities.

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

`search` can be `syntactic`, `semantic`, or `none`; `boost` is a positive
relative ranking weight. Validation checks required fields and can lightly
check a declared edge against an existing target type. See the
[cookbooks](./cookbooks.md) for end-to-end patterns.

## Add the surfaces you need

```bash
# Read-only map and search explorer
python -m pip install '.[ui]'
droid-brain ui --brain examples/support-brain

# Local semantic embeddings and hybrid retrieval
python -m pip install '.[semantic]'
droid-brain index --brain examples/support-brain --reembed
```

The local semantic default is `BAAI/bge-small-en-v1.5` (384 dimensions). For an
OpenAI-compatible embedding service, install `.[semantic]` and set all four
variables before indexing or searching:

```bash
export DROID_BRAIN_EMBEDDING_BASE_URL="https://embeddings.example.invalid/v1"
export DROID_BRAIN_EMBEDDING_API_KEY="${EMBEDDING_API_KEY}"
export DROID_BRAIN_EMBEDDING_MODEL="embedding-model-name"
export DROID_BRAIN_EMBEDDING_DIM="384"
```

Set `search.semantic_weight` to blend keyword and semantic scores, and rerun
`droid-brain index --brain <path> --reembed` after changing model or dimensions.

For HTTP MCP, install the serving extra immediately before launching it. Add the
OpenSearch extra only for a brain configured with that backend:

```bash
python -m pip install '.[serve]'
python -m pip install '.[opensearch]'  # only for a configured OpenSearch backend
```

## Operational notes

**Current-state storage.** File-backed entities are JSON under
`entities/<doc_type>/`, making them Git-versionable when an operator commits
changes. Index-backed entities live in the configured backend. A same-ID write
replaces the desired current entity, so send complete fields and outgoing edges;
use one writer per ID namespace or external serialization when writers may
compete. For provenance, run manifests, replacement behavior, and recovery,
see [entity management](./entity-management.md).

**Refresh and retrieval.** Connectors update the IDs they emit. Model explicit
resolved/inactive states or reconcile externally when absence should change
current state; use run-scoped IDs when you need history. SQLite keyword reranking
is capped at 500 candidates and SQLite semantic/hybrid scanning at 10,000
entities; OpenSearch all-entity and relationship scans also cap at 10,000. These
are implementation bounds, not quality or capacity claims. More patterns and
search guidance are in the [cookbooks](./cookbooks.md); future lifecycle,
policy, vector-indexing, and benchmark work is in the [roadmap](./ROADMAP.md).

## Authentication is not authorization

HTTP MCP can require one shared bearer token, checked on every request. Local
stdio relies on the local process/user boundary, and `--read-only` removes write
tools for an entire endpoint. These are useful deployment controls, not
per-caller or multi-tenant authorization: prompts, `doc_types`, `tenant_id`,
`visibility`, ACL-like fields, and OpenSearch credentials do not enforce access.

For distinct trust boundaries, run separate brain directories and SQLite
databases or OpenSearch indexes, with separate processes, endpoints, and tokens.
Keep read-only and read/write endpoints separate, use least-privileged process
and backend identities, and place networked endpoints behind your own TLS,
gateway, and network controls. Fail closed if a token is absent:

```bash
# Bind locally for an external gateway; shell expansion fails when a token is unset.
DROID_BRAIN_TOKEN="${WORKSPACE_A_READ_TOKEN:?must be set}" \
  droid-brain serve --brain /srv/droid-brain/workspace-a --host 127.0.0.1 --port 8101 --read-only
DROID_BRAIN_TOKEN="${WORKSPACE_A_WRITE_TOKEN:?must be set}" \
  droid-brain serve --brain /srv/droid-brain/workspace-a --host 127.0.0.1 --port 8102
```

This is coarse deployment segregation, not in-brain authorization. See the
[authorization roadmap](./ROADMAP.md#first-class-policy-aware-access)
and the [deployment cookbook](./cookbooks.md#6-deploy-isolated-knowledge-boundaries).

## Examples and project links

- [Support](./examples/support-brain): products, issues, user segments, and comments.
- [Infrastructure](./examples/infra-brain): services, datastores, dashboards, runbooks, and refreshed alerts.
- [Personal](./examples/personal-brain): areas, goals, projects, people, and notes.
- [Cloud](./examples/cloud-brain): Kubernetes-style infrastructure entities and a connector.
- [Cookbooks](./cookbooks.md) · [Entity management](./entity-management.md) · [Roadmap](./ROADMAP.md)
- [Contributing](./CONTRIBUTING.md) · [MIT License](./LICENSE)
