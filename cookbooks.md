# Droid Brain cookbooks

These recipes use the repository checkout as the working directory. The bundled
data is synthetic. A recipe describes current behavior; it is not a promise of
workflow automation, policy enforcement, or historical versioning that the
running brain does not provide.

## Choose a recipe

| Agent goal | Bundled example | Source-of-truth pattern | Required install extra | Demonstrated behavior |
|---|---|---|---|---|
| Triage a support question | `examples/support-brain` | Curated `storage: file` entities plus SQLite search | None; `[mcp]` for an agent connection | Index, terminal search, and an MCP read sequence |
| Investigate a service alert | `examples/infra-brain` | Curated files plus index-backed, periodically refreshed alerts | None; `[mcp]` or `[ui]` only for those interfaces | Offline alert ingestion, search, and relationship-aware investigation |
| Write controlled distilled memories | A new, purpose-specific brain | Small reviewed files, or high-volume index records with an external manifest | `[mcp]` only to use MCP write tools | Provenance convention, immutable external run record, and complete replacement write |
| Refresh an upstream source | `examples/support-brain/connectors/example_connector.py` | Connector-owned index or file entities | None | Deterministic source-to-entity mapping, due-state, and per-record errors |
| Add semantic/hybrid retrieval | Any brain with `search: semantic` fields | Existing storage backend plus embeddings | `[semantic]`; `[opensearch]` when using OpenSearch | Local or compatible remote embeddings and re-embedding |
| Separate untrusted audiences | One brain directory and backend per boundary | Operationally separate deployments | `[serve]`; `[opensearch]` for an OpenSearch backend | Separate read/read-write endpoints and external gateway controls |

Install the base package once before any recipe:

```bash
python -m pip install .
```

## 1. Support triage agent

### Goal

Answer a support question from the curated products, issues, user segments, and
comments in `examples/support-brain`.

### Why this model

The support example's entities are `storage: file`, so the JSON under
`entities/` is the current, reviewable source of truth. SQLite is the local
search index. This is appropriate when the support knowledge is curated rather
than a high-volume operational feed.

### Install

No extra is required for terminal indexing and search. Install MCP only when a
local agent will call the MCP tools:

```bash
python -m pip install '.[mcp]'
```

### Setup

The bundled support brain deliberately has **no** `.mcp.json`. Verify that and
create a client configuration in the directory where your MCP client expects
one; do not add it to the bundled example merely to run this recipe.

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

`droid-brain init my-new-brain` generates `.mcp.json` for that **new** brain.
It does not retroactively create client configuration for an existing bundled
example.

### Run

```bash
droid-brain index --brain examples/support-brain
droid-brain search "payment declined" --brain examples/support-brain --doc-type issue
```

For an MCP-capable agent, use this read path in order:

1. `navigation_guidelines()` to learn the available doc types, fields, and
   relationships.
2. `search_brain(query="payment declined", doc_types=["issue"], limit=5)` to
   locate candidate issues.
3. `get_entity(entity_id="issue:payment-declined")` for the complete
   entity and its incoming/outgoing relationships.

### Expected result

Indexing loads the bundled file-backed entities. Terminal search returns the
synthetic payment-declined issue; the MCP sequence gives the agent schema
context before it narrows to and inspects an entity.

### Persistence/history

Edits to a file-backed entity made through the CLI or MCP replace its complete
JSON file and its indexed current value. Commit reviewed JSON changes if a
recoverable revision is needed. `droid-brain index` reloads the file-backed
types from disk.

### Current limits

This is a knowledge lookup workflow, not a ticketing integration or a policy
engine. MCP client configuration and any agent instructions are local client
configuration, not access control. The agent should treat search results as
leads and use `get_entity` before drawing a conclusion.

## 2. Infrastructure troubleshooting agent

### Goal

Investigate synthetic current alerts with services, dashboards, runbooks, and
datastores from `examples/infra-brain`.

### Why this model

The stable infrastructure map is file-backed, while `alert` is `storage: index`.
The `infra-alerts` connector therefore writes operational alert records to the
SQLite index without creating alert JSON files in Git. It models alerts as
**periodically refreshed/upserted current records**, not as a historical feed.

### Install

Install only the interface you will use:

```bash
# To let a local agent use MCP tools
python -m pip install '.[mcp]'

# Immediately before launching the optional map explorer
python -m pip install '.[ui]'
```

### Setup

The included `infra-alerts` connector has an offline sample, so no alert-system
credentials or network endpoint are required for this recipe. Its configured
schedule is `hourly`.

### Run

```bash
droid-brain index --brain examples/infra-brain
droid-brain ingest infra-alerts --brain examples/infra-brain
droid-brain search "Checkout 5xx" --brain examples/infra-brain --doc-type alert
droid-brain run --brain examples/infra-brain
```

For a local MCP investigation after installing `[mcp]`, run
`droid-brain mcp --brain examples/infra-brain` and use
`navigation_guidelines` → `search_brain` → `get_entity`. Use the alert's
`fires on` relationship to inspect the linked service and then its runbooks or
dashboards.

To explore the map after installing `[ui]`:

```bash
droid-brain ui --brain examples/infra-brain
```

### Expected result

`ingest` writes the two synthetic alerts, including `alert:checkout-5xx` and
its `fires on` edge. The first `run` executes a due connector; later calls can
report it as not due until the stored interval elapses.

### Persistence/history

Each connector run replaces only the IDs that it emits. An omitted upstream
alert remains persisted and can become stale. Have the source emit an explicit
`resolved` or `inactive` state, or perform an external full reconciliation or
rebuild when absence must change current state. For historical observations,
emit separate run-scoped IDs such as
`alert_observation:run-2026-08-07T120000Z:checkout-5xx` and link each to the
stable alert; ordinary `alert:checkout-5xx` is only its latest current value.

Back up the SQLite database or make the connector ingestion repeatable: alert
records are index-backed and are not entity JSON files.

### Current limits

The offline connector is demonstration data, not a live alert feed. A due run
tracks connector cadence; it does not establish freshness or remove missing
source records. SQLite is local storage and does not turn this into a
multi-writer incident system.

## 3. Controlled AI distillation or memory write-back

### Goal

Let an external agent transform selected source material into a validated,
traceable current memory while retaining enough external evidence to review or
replay the transformation.

### Why this model

Droid Brain stores a current entity, not an immutable extraction ledger. Treat
the model invocation, source selection, and retained artifacts as an external
workflow; use ordinary schema fields for provenance and an immutable external
manifest for each run.

### Install

Install MCP for the write tool:

```bash
python -m pip install '.[mcp]'
```

### Setup

Define provenance fields on the target doc type. They are user-defined,
non-reserved fields; `search: none` keeps metadata out of retrieval. This
synthetic `memory` type is file-backed for a small reviewable workflow. Create
a named disposable brain, save this file as `$DISTILL_BRAIN/doc_types/memory.yaml`,
then index and validate it:

```bash
DISTILL_BRAIN="$(mktemp -d "${TMPDIR:-/tmp}/droid-brain-distillation.XXXXXX")"
droid-brain init distillation-brain "$DISTILL_BRAIN"
```

```yaml
doc_type: memory
description: A distilled operational memory with external-run provenance.
storage: file
display:
  label_field: name
  color: "#2563eb"
schema:
  fields:
    - { name: summary, type: text, processing: text, search: semantic }
    - { name: source_system, type: string, processing: keyword, search: none }
    - { name: source_record_id, type: string, processing: keyword, search: none }
    - { name: source_revision, type: string, processing: keyword, search: none }
    - { name: source_content_hash, type: string, processing: keyword, search: none }
    - { name: source_uri, type: string, processing: keyword, search: none }
    - { name: observed_at, type: timestamp, processing: timestamp, search: none }
```

```bash
droid-brain index --brain "$DISTILL_BRAIN"
droid-brain validate --brain "$DISTILL_BRAIN"
```

`init` creates `$DISTILL_BRAIN/.mcp.json` for the new brain. Open the MCP client
with `$DISTILL_BRAIN` as its working directory so its generated `--brain .`
configuration resolves correctly, or start the same stdio server explicitly:

```bash
droid-brain mcp --brain "$DISTILL_BRAIN"
```

Before a write, the external workflow should record one immutable manifest per
run. This complete synthetic JSON example has all the fields needed to identify
inputs, code, prompt, model settings, output, and retained recovery material;
Droid Brain neither creates nor validates it.

```json
{
  "run_id": "distill-2026-08-07T120000Z-001",
  "run_timestamp_utc": "2026-08-07T12:00:00Z",
  "sources": [
    {
      "source_system": "synthetic-support-export",
      "source_id": "case-482",
      "source_revision": "rev-17",
      "source_uri": "https://support.example.invalid/cases/482",
      "source_content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ],
  "extractor": {
    "repository": "https://git.example.invalid/knowledge/distiller",
    "code_commit": "4f6c3a1"
  },
  "prompt": {
    "template_name": "support-memory-distillation",
    "template_version": "2026-08-01",
    "template_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "generation": {
    "provider": "synthetic-compatible-provider",
    "model": "synthetic-model-1",
    "temperature": 0.0,
    "top_p": 1.0,
    "seed": 42,
    "max_tokens": 800,
    "structured_output_schema_version": "memory-v1"
  },
  "outputs": [
    {
      "entity_id": "memory:wallet-eu-guidance",
      "content_hash": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  ],
  "retained_artifacts": {
    "prior_entity_versions": ["s3://knowledge-audit.invalid/prior/memory-wallet-eu-guidance-v3.json"],
    "source_artifacts": ["s3://knowledge-audit.invalid/sources/case-482-rev-17.json"],
    "store_snapshots": ["s3://knowledge-audit.invalid/stores/distill-2026-08-07T120000Z-001.sqlite"],
    "manifest_uri": "s3://knowledge-audit.invalid/manifests/distill-2026-08-07T120000Z-001.json"
  }
}
```

Set provider credentials in the external agent's environment, for example
`${LLM_API_KEY}`. Do not place source credentials or provider keys in entity
JSON, MCP configuration, or the manifest.

### Run

After the agent has called `navigation_guidelines`, send a complete MCP
`put_entity` argument payload such as:

```json
{
  "doc_type": "memory",
  "id": "memory:wallet-eu-guidance",
  "name": "EU wallet payment guidance",
  "fields": {
    "summary": "Wallet payments are unavailable for the synthetic EU checkout path; offer a supported payment method and link the incident runbook.",
    "source_system": "synthetic-support-export",
    "source_record_id": "case-482",
    "source_revision": "rev-17",
    "source_content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "source_uri": "https://support.example.invalid/cases/482",
    "observed_at": "2026-08-07T12:00:00Z"
  },
  "related_to": [
    {
      "target": "issue:wallet-not-supported",
      "relationship_edge_meaning": "distilled from"
    }
  ]
}
```

### Expected result

The write is validated and persists to
`entities/memory/wallet-eu-guidance.json` for this file-backed type. The MCP
result reports that path. The external manifest remains the evidence for how
the entity was generated.

### Persistence/history

A write with the same ID replaces the complete current entity: omitted fields
and outgoing relationships are discarded. Preserve a prior value outside the
brain before replacement when it matters.

For small, reviewable output, use `storage: file`, commit the entity and the
immutable manifest revision together, and record the resulting commit in the
external run record. For larger output, use `storage: index` for current state
and retain an append-only manifest/object store plus source artifacts and a
SQLite/OpenSearch store backup sufficient to replay the run.

The scaffold's reflection/write-back hook is only a stub: an external agent
workflow decides when to extract, validates the proposed payload, retains the
manifest, and calls `put_entity`. No in-process hook autonomously runs or
approves a reflection.

### Current limits

There is no built-in provenance validation, immutable entity versioning,
approval workflow, conflict resolution, or write precondition. Read-construct-
write can help an agent send a complete payload, but concurrent writers can
still lose an intervening update.

## 4. Connector refresh

### Goal

Adapt `examples/support-brain/connectors/example_connector.py` to map an
upstream support system into complete, deterministic entities.

### Why this model

A connector is ordinary Python. It owns the source-to-entity mapping, runs each
`extract_*` method, and sends every `EntitySpec` through the same validation and
write path as other writers. The runner expands `${ENV_VAR}` references in the
connector's MCP URL and authentication headers at runtime.

### Install

No extra is required.

### Setup

Replace the example connector's source declaration and mapping with a stable
source identifier, complete fields, and explicit source state. This synthetic
adaptation uses environment-expanded source credentials; it does not put a
secret in the file.

```python
from droid_brain.connectors import Connector, EntitySpec


class SupportIssuesConnector(Connector):
    name = "support-issues"
    mcp_url = "${SUPPORT_SOURCE_MCP_URL}"
    mcp_auth_headers = {"Authorization": "Bearer ${SUPPORT_SOURCE_TOKEN}"}
    schedule = "6h"
    tool_name = "list_issues"
    target_doc_type = "issue"

    def extract_issues(self):
        for item in self.paginate(self.tool_name, {"include_resolved": True}, result_key="issues"):
            source_id = str(item["id"])
            product_id = item.get("product")
            related_to = []
            if product_id:
                related_to.append((f"product:{product_id}", "affects product"))
            yield EntitySpec(
                doc_type=self.target_doc_type,
                id=f"issue:{source_id}",
                name=item["title"],
                fields={
                    "description": item.get("description", ""),
                    "severity": item.get("severity", "unknown"),
                    "status": item.get("state", "open")
                },
                related_to=related_to,
            )
```

Use one owner for an ID namespace such as `issue:<source-id>`. If more than one
producer must write it, serialize writes with an external queue, lock, or
transactional coordinator. A retry is idempotent **only when it resends the
same complete entity** (including fields and `related_to`); a partial or newer
payload is a replacement, not a merge.

### Run

Use the bundled offline demo before editing it:

```bash
droid-brain ingest example-issues --brain examples/support-brain
droid-brain run --brain examples/support-brain
```

After installing the adaptation and exporting the source values in the process
environment, use its connector name:

```bash
droid-brain ingest support-issues --brain examples/support-brain
droid-brain run --brain examples/support-brain
```

`run` evaluates the declared interval and records connector due-state in the
brain's local run-state file. It is an interval evaluator; arrange the invoking
process with your own scheduler or CI system if recurring execution is needed.

### Expected result

Each valid record becomes one complete `issue:<source-id>` entity. After an
`EntitySpec` has successfully constructed an entity, the runner continues after
a `Brain.put_entity` validation/write failure and reports that entity in its
errors while accepting other constructed entities. Malformed specs or failures
during `EntitySpec.to_entity()` construction can abort the run, as can a
source/MCP connection failure.

### Persistence/history

Connector writes replace only the IDs emitted in that run. An upstream record
that is absent from a result remains in the brain, so do not infer deletion
from absence. Prefer an explicit source `resolved`, `inactive`, or `deleted`
state in the complete emitted payload, or execute an external reconciliation or
rebuild when the source supports authoritative enumeration.

File-backed issue entities are persisted as JSON; index-backed targets are
durable backend state and need backup, repeatable ingestion, or an external
immutable record for recovery.

### Current limits

There is no built-in distributed lock, transaction across the source and brain,
absence-as-deletion behavior, or automatic historical record of each run. A
connector's schedule records due-state only; it is not evidence that all source
records were fetched successfully.

## 5. Optional hybrid retrieval

### Goal

Blend keyword matches with semantic similarity for doc types that declare
`search: semantic` fields.

### Why this model

Base installation retains keyword search. If a semantic field is in scope but
no embedding provider is available, the backend warns and falls back to keyword
search. SQLite semantic/hybrid retrieval requires the semantic optional
dependency for both local and remote embedding providers because its scoring
path uses NumPy. OpenSearch remote retrieval instead requires the OpenSearch
extra plus a complete remote provider configuration.

### Install

For SQLite semantic retrieval, install:

```bash
python -m pip install '.[semantic]'
```

### Setup

The local provider defaults to `BAAI/bge-small-en-v1.5`. Set a local model in
the brain configuration when a different compatible model is required:

```yaml
search:
  backend: sqlite
  semantic_weight: 0.3
  embedding_model: BAAI/bge-small-en-v1.5
```

For a remote OpenAI-compatible provider, configure all four values in the
invoking process environment. Keep `.[semantic]` installed when the search
backend is SQLite. With OpenSearch, install `.[opensearch]` instead:

```bash
python -m pip install '.[opensearch]'
```

```bash
export DROID_BRAIN_EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL}"
export DROID_BRAIN_EMBEDDING_API_KEY="${EMBEDDING_API_KEY}"
export DROID_BRAIN_EMBEDDING_MODEL="${EMBEDDING_MODEL}"
export DROID_BRAIN_EMBEDDING_DIM="${EMBEDDING_DIM}"
```

`semantic_weight: 0` is keyword-only, `1` is semantic-only, and the default
`0.3` retains keyword as the stronger signal. A field must be declared
`search: semantic` to be embedded.

### Run

```bash
droid-brain index --brain examples/support-brain --reembed
droid-brain search "payment method unavailable" --brain examples/support-brain --doc-type issue
```

Run `droid-brain index --reembed` after adding semantic fields, changing the
provider/model, or changing embedding dimension. For OpenSearch, recreate the
index before a full re-embedding when the dimension changes.

### Expected result

The index stores embeddings for semantic fields and queries blend the keyword
and semantic scores according to `semantic_weight`. Without an available local
or remote provider, the same query remains a keyword query after a warning.

### Persistence/history

Embeddings are derived index artifacts, not a source-of-truth version history.
Record the provider, model, dimension, and rebuild procedure with the data
backup or external run record so the index can be reproduced.

### Current limits

SQLite reranks at most 500 FTS candidates for keyword search and caps its
brute-force semantic scan at 10,000 entities. OpenSearch uses the shipped k-NN
query path for the semantic arm. These implementation caps are not performance,
recall, latency, or relevance guarantees; measure the workload and backend you
actually deploy.

## 6. Policy-gated or multi-tenant knowledge deployment today

### Goal

Deploy knowledge for distinct trust boundaries without implying that a single
brain has tenant-aware authorization.

### Why this model

Start by defining boundaries such as `workspace-a` versus `workspace-b`, or
`public` versus `internal` versus `restricted`. Give every boundary a separate
brain directory and dedicated SQLite database, or a separate OpenSearch
index/backend. This is operational/physical segregation, not in-brain RBAC,
ABAC, or tenant policy.

### Install

Install the HTTP endpoint support:

```bash
python -m pip install '.[serve]'
```

### Setup

For example, establish separate roots and SQLite database paths; the contents
and credentials for one boundary must not be copied into another:

```text
/srv/droid-brain/workspace-a/brain.yaml    -> storage.path: ./workspace-a.db
/srv/droid-brain/workspace-b/brain.yaml    -> storage.path: ./workspace-b.db
```

For a shared backend instead, use a separate OpenSearch index and least-
privileged backend identity for each boundary:

```bash
python -m pip install '.[opensearch]'
```

```yaml
search:
  backend: opensearch
  hosts: ["${WORKSPACE_A_OPENSEARCH_URL}"]
  index: droid_brain_workspace_a
  username: "${WORKSPACE_A_OPENSEARCH_USER}"
  password: "${WORKSPACE_A_OPENSEARCH_PASSWORD}"
  use_ssl: true
  verify_certs: true
```

Run processes under distinct least-privileged OS/container identities and
dedicated backend identities. Isolate connector source credentials and write
destinations, backups, exports, and embedding/index artifacts by the same
boundary.

### Run

When readers and writers differ, expose separate endpoints with distinct static
tokens rather than sharing one writable token. These commands bind local ports
for an external gateway to route; they are examples, not public endpoints to
launch from a development shell.

```bash
# Workspace A reader: read tools only, token distinct from the writer token.
DROID_BRAIN_TOKEN="${WORKSPACE_A_READ_TOKEN:?must be set}" \
  droid-brain serve --brain /srv/droid-brain/workspace-a --host 127.0.0.1 --port 8101 --read-only

# Workspace A writer: read + write tools, separately routed and authenticated.
DROID_BRAIN_TOKEN="${WORKSPACE_A_WRITE_TOKEN:?must be set}" \
  droid-brain serve --brain /srv/droid-brain/workspace-a --host 127.0.0.1 --port 8102

# Workspace B is a separate trust boundary with separate brain and tokens.
DROID_BRAIN_TOKEN="${WORKSPACE_B_READ_TOKEN:?must be set}" \
  droid-brain serve --brain /srv/droid-brain/workspace-b --host 127.0.0.1 --port 8201 --read-only
```

Put an external TLS/authentication gateway in front of these local endpoints.
The gateway should integrate caller identity, route only to the permitted
boundary endpoint, apply network controls, rotate tokens, rate-limit where
needed, and retain durable request/audit logs.

### Expected result

`--read-only` removes `put_entity` and `create_doc_type` from that process;
the write endpoint retains them. The optional bearer token establishes that a
request presented the endpoint's shared token. Authentication establishes
who/what may connect under the gateway's scheme; authorization decides which
actions and resources that principal may access. The built-in static token is a
uniform endpoint gate, not resource authorization.

### Persistence/history

Each separate brain/backend has independent current data and recovery material.
Back up and export each boundary independently; do not combine stores or
embedding artifacts when doing so would cross the defined boundary.

### Current limits

There is no principal/claims model, tenant/workspace isolation inside a brain,
per-brain/doc-type/entity/field/relationship/source ACL, RBAC/ABAC, policy
engine, row-level filtering, write ownership, policy-aware audit trail, or
common deny-by-default check on all data paths. `--read-only` is process-wide,
not per-principal authorization.

> **Anti-patterns — not authorization**
>
> Never treat an agent prompt such as “only answer for tenant A”, a caller-
> provided `doc_types` filter, `tenant_id`, `visibility`, or ACL-like fields,
> entity name prefixes, or OpenSearch credentials as authorization. Prompts are
> advisory; `doc_types` scopes a query; those fields and prefixes are ordinary
> data; OpenSearch credentials secure backend connectivity. None enforces what
> an application caller may read or write. A real policy would need the same
> enforced decision on get, search, count, navigation, incoming/outgoing
> relationship traversal, writes, exports, and every related data path. Current
> Droid Brain does not provide that common enforcement.

## Modeling ideas, not runnable recipes

The following are starting points for schema design, not shipped integrations
or commands:

- **Business process and organization brain:** model `team`, `role`, `system`,
  `process`, `decision`, and `runbook` entities. Relate a process to its owner,
  supporting systems, controls, and escalation runbook. Decide which are
  curated file records versus regenerated index records before ingesting data.
- **Sales-lifecycle brain:** model `account`, `contact`, `lead`, `opportunity`,
  `stage`, `activity`, and `order`; relate each opportunity to its account,
  contacts, current stage, and material activities. Define stable CRM-derived
  IDs, source ownership, explicit closed/lost state, and recovery artifacts
  before building a connector or agent write-back workflow.
