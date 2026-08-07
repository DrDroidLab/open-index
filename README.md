### Open Index

[![Join our Discord](https://img.shields.io/badge/Discord-join%20the%20community-5865F2?logo=discord&logoColor=white)](https://discord.gg/AQ3tusPtZn)

**Open Index** is a tool for building domain specific accurate, structured data that agents can actually operate on — and for keeping that data correct as things change.

You use Open Index to build a **brain**: a searchable, continuously-improving context graph of your domain. A brain is **domain-agnostic** — model a support org (`product → "has common issue" → issue`), a sales pipeline (`customer → order`), your infrastructure (`service → runbook`), or anything else. You define the concepts; Open Index stores them, searches them, and draws the map.

A brain is built from four primitives:
- **doc_type** — a concept you want to track and maintain (e.g. `service`, `customer`, `issue`).
- **doc_schema** — the fields stored for a given doc_type.
- **entity** — one instance of a doc_type, stored per its schema. Every entity can link to others via `related_to` (the target) + `relationship_edge_meaning` (free-text edge semantics).
- **connector** — an optional source you extract entities from (e.g. an MCP server).


## Quickstart

```bash
pip install -e '.[all]'          # core + UI (Streamlit) + MCP server

# Try the bundled example (support brain: products, issues, segments, comments)
open-index index --brain examples/support-brain
open-index ui    --brain examples/support-brain      # open the Map tab, pick an anchor

# Or start your own brain from scratch
open-index init my-brain
open-index add-doc-type customer --brain my-brain
# ...add entities under my-brain/entities/**/*.json...
open-index index --brain my-brain
open-index ui    --brain my-brain
```

### Commands

| Command | What it does |
|---|---|
| `open-index init <name> [dir]` | Scaffold a new brain directory. |
| `open-index add-doc-type <name>` | Add a doc_type schema stub under `doc_types/`. |
| `open-index add-entity <file>` | Validate + store an entity JSON file. |
| `open-index index` | (Re)load `entities/**/*.json` into the search index. |
| `open-index validate` | Validate `brain.yaml`, schemas, and every entity file (use in CI). |
| `open-index ingest <connector>` | Run a connector now to pull entities from an MCP server. |
| `open-index run [--force] [--loop N]` | Run every connector whose `schedule` is due (wire into cron/CI). |
| `open-index search <query> [-t doc_type]` | Search from the terminal. |
| `open-index ui` | Launch the Streamlit explorer (Structure / Search / **Map** / Contribute). |
| `open-index mcp` | Run the MCP server (stdio) so a local agent can **read and write** the brain. |
| `open-index serve [--port --token]` | Serve the MCP server over **HTTP** for remote/cloud agents (bearer-token auth). |

### Built for Claude Code (and any agent)

`open-index init` also drops a `.mcp.json` and a `CLAUDE.md` in the brain, so the
moment you open Claude Code in that folder it can drive the brain over MCP:

- **read** — `navigation_guidelines()` (what doc_types exist + how to query), `search_brain()`, `get_entity()`
- **write** — `put_entity()` (add/update an entity), `create_doc_type()` (define a concept)

So you *define* doc_types and *populate* entities by talking to the agent — it
writes the YAML/JSON files (the git source of truth) for you. Entities also arrive
via manual JSON, connectors (MCP ingestion on a `schedule`), or an agent
write-back loop (a Stop hook that records learnings via `put_entity`).

### A brain on disk

```
my-brain/
  brain.yaml            # name + storage/search backend
  doc_types/*.yaml      # one schema per doc_type (fields, boosts, display color)
  entities/**/*.json    # entities, with related_to edges
  connectors/*.py       # optional ingestion scripts (MCP → entities)
```

Storage defaults to **SQLite + FTS5** (zero external services). The backend sits
behind a pluggable interface with two implementations: SQLite (default, local/dev)
and **OpenSearch** (select with `search.backend: opensearch` — see
[Using the brain from a cloud agent](#using-the-brain-from-a-cloud-agent-production)).

### Where entities live — `storage: file | index`

Each doc_type declares its source of truth, so curated and machine-generated data
don't fight over git:

- **`storage: index`** (default) — the search DB owns these entities; they are
  **not** written to files. Right for connector-pulled, high-volume, or temporal
  data (hundreds of services, memories, alerts) that would otherwise churn the repo.
- **`storage: file`** — JSON files under `entities/<doc_type>/` are the source of
  truth, git-tracked and PR-reviewable. Right for curated, human/agent-authored
  entities.

`open-index index` reconciles **file**-backed types from disk on each run and
leaves **index**-backed entities (written by connectors/agents) untouched. So
`brain.db` is durable state for index-backed types — back it up or re-ingest;
it's gitignored by default.


# Creating a brain, step by step

`open-index init <name>` scaffolds the directory below; then you author two kinds
of file — **doc_types** (schemas) and **entities** (instances). Sample doc_types:
infra (`service`, `datastore`, `dashboard`, `runbook`, `alert`), sales (`lead`,
`deal`, `account`), lending (`loan`, `borrower`, `application`), or personal
(`goal`, `project`, `person`, `area`, `note`). Three runnable examples ship in
[`examples/`](./examples): `support-brain`, `infra-brain`, and `personal-brain`.

### 1. Define a doc_type

A doc_type is a concept plus its schema — one YAML file in `doc_types/`:

```yaml
# doc_types/service.yaml
doc_type: service
description: A deployed service.
storage: file                 # file = git source of truth · index = DB-owned (default)
display:
  label_field: name
  color: "#7c3aed"
schema:
  fields:
    - { name: name,        type: string, search: syntactic, boost: 6 }   # weighted 6× in ranking
    - { name: description, type: text,   search: semantic }
    - { name: owner,       type: string, search: syntactic }
relationships:                # the correlations this type uses — optional but recommended
  - { name: "writes to",       target_doc_type: datastore }
  - { name: "is monitored by", target_doc_type: dashboard }
```

- **`boost`** sets per-field search weight — a hit in a `boost: 6` title outranks a
  `boost: 1` description hit 6-to-1. Optional; defaults to 1.
- **`relationships`** declares the edge vocabulary so correlations are discoverable
  (shown in the UI + navigation guide) and lightly validated (right target type).
  Optional — entities may still use undeclared meanings.

Create one with `open-index add-doc-type service` (writes a stub you edit), or ask your agent.

### 2. Add entities

An entity is one instance. For `storage: file` types, write one JSON per entity
under `entities/<doc_type>/`:

```json
// entities/service/checkout.json
{
  "doc_type": "service",
  "id": "service:checkout",
  "name": "Checkout",
  "owner": "payments-team",
  "related_to": [
    { "target": "datastore:postgres-main",     "relationship_edge_meaning": "writes to" },
    { "target": "dashboard:checkout-latency",   "relationship_edge_meaning": "is monitored by" }
  ]
}
```

- `id` must be `<doc_type>:<slug>`.
- **`related_to`** is the reserved correlation field present on **every** entity — it
  defines the graph edges (`target` + `relationship_edge_meaning`). This is how you
  say "this ticket is about that service" without any graph database.

Then `open-index index` (loads file-backed entities) and `open-index validate`.

### 3. Populate at scale (four ways, one validated store)

1. **Manual / agent** — write JSON, or open Claude Code in the folder and let it call
   `put_entity` / `create_doc_type` over MCP.
2. **Bulk** — hand a file of records to your agent, or a connector.
3. **Connectors** — `connectors/*.py` pull from an MCP server on a `schedule`; run with
   `open-index ingest <name>` or `open-index run` (cron/CI-friendly).
4. **Agent write-back** — a Stop hook that records learnings via `put_entity` (the
   "continuously improving" loop).

See [Entity Management](./entity-management.md) for guidance on cadence and decay.

### 4. Explore

`open-index ui` → **Structure** (doc_types, fields, relationships), **Search**, and
**Map** (anchor a doc_type, pick entities, click a node to expand its correlations).

# Enabling your agent to use the brain

`open-index mcp` runs an MCP server (stdio) exposing the brain to any MCP client —
**read and write**:

- `navigation_guidelines()` — orient: doc_types, fields, relationships, how to query/write.
- `search_brain(query, doc_types, limit)` · `get_entity(id)` — read.
- `put_entity(...)` · `create_doc_type(...)` — write (validated, honors the storage policy).

`open-index init` drops a `.mcp.json` so Claude Code auto-connects, a `CLAUDE.md`
documenting the model, and an **`edit-brain` skill** (`.claude/skills/edit-brain/SKILL.md`)
the agent follows when adding or correlating knowledge. The UI's **Contribute** tab
shows how to connect — editing happens via the agent or CLI, never in the UI, so files
stay the source of truth and every write is validated.

## Using the brain from a cloud agent (production)

Two shapes, depending on whether one agent or many share the brain:

**Embedded (one agent, you own the runtime).** Bake the package + brain dir into the
agent's image and let it spawn the MCP server over stdio — no network, works today:

```jsonc
{ "mcpServers": { "brain": { "command": "open-index", "args": ["mcp", "--brain", "/app/brain"] } } }
```

**Remote (many agents, shared brain).** Run the brain as a networked MCP server and
register its URL in each agent:

```bash
pip install 'open-index[serve,opensearch]'
OPEN_INDEX_TOKEN=… open-index serve --brain /srv/acme-brain --port 8080
# agent registers a remote MCP server:  http://<host>:8080/mcp
#                                        Authorization: Bearer <OPEN_INDEX_TOKEN>
```

`open-index serve` exposes the same read+write tools over **streamable HTTP** with
**bearer-token auth**. For a shared, multi-writer brain, switch the backend to
**OpenSearch** (SQLite is single-writer) — flip `search.backend` in `brain.yaml`:

```yaml
search:
  backend: opensearch
  hosts: ["https://opensearch:9200"]
  index: open_index_acme          # optional; defaults to open_index_<name>
  username: "${OPENSEARCH_USER}"    # ${ENV} resolved at connect time
  password: "${OPENSEARCH_PASSWORD}"
  use_ssl: true
  verify_certs: true
```

OpenSearch also gives native per-field boosting and **fuzzy** (typo-tolerant) search.
The doc_type/file-entity part comes from git; index-backed data lives in the cluster,
so give it a persistent home. Rule of thumb: **local/dev → SQLite; exposed as an
MCP/API endpoint → OpenSearch + `serve`.**

# Controlling search

**Schema** (per field): data `type` (string/number/boolean/timestamp), `processing`
(keyword/text/timestamp), and `search` kind (`syntactic` = keyword+prefix,
`semantic` = vector-backed dense search, `none` = not indexed).
Mark a field `search: semantic` and the backend automatically embeds it at index time.

**Ranking** — genuine **per-field boosters**: each field's `boost` weights how much a
match there counts, so you tune "title matters more than description" with one number.
For hybrid queries, keyword and semantic scores are blended with `search.semantic_weight`
(default `0.3` — keyword matches dominate; semantic similarity rescues queries that use
different words than the text). `semantic_weight: 0` gives keyword-only behavior; `1.0`
gives semantic-only.
Storage defaults to SQLite + FTS5; the OpenSearch backend implements the same
interface with native per-field boosting, fuzzy matching, and k-NN semantic search.

**Embedding model** — install the `[semantic]` extra (`pip install 'open-index[semantic]'`)
to enable local embeddings. The default model is `BAAI/bge-small-en-v1.5` (384-D). Override
it with `search.embedding_model` in `brain.yaml`, or use an OpenAI-compatible API by setting
`OPEN_INDEX_EMBEDDING_BASE_URL`, `OPEN_INDEX_EMBEDDING_API_KEY`, `OPEN_INDEX_EMBEDDING_MODEL`,
and `OPEN_INDEX_EMBEDDING_DIM`.

Changing the embedding dimension (e.g., switching from the local 384-D model to a 512-D API
provider) requires rebuilding the index: `open-index index --reembed` on SQLite, or a full
`open-index index --reembed` on OpenSearch after recreating the index.

**Re-embedding** — the reserved field `embedding` stores the per-entity vector. If you enable
semantic search on an existing index, run `open-index index --reembed` to backfill vectors.

**SQLite semantic ceiling** — the SQLite backend performs a brute-force cosine scan over the
entities in scope. This is fine up to roughly **10,000 entities**; for larger brains, switch to
the OpenSearch backend or a future `sqlite-vec` integration.

_Not yet implemented (declarable seams exist): type-level & temporal boosters._

# Contributing

Contributions are welcome — new doc_type examples, connectors, backends, docs fixes,
or bug reports all help.

**Join the community on [Discord](https://discord.gg/AQ3tusPtZn)** to ask questions,
share the brains you're modelling, or discuss an idea before you build it. It's the
fastest way to get an answer and the best place to sanity-check a bigger change.

### Getting set up

```bash
git clone https://github.com/DrDroidLab/open-index
cd open-index
pip install -e '.[all]'          # core + UI (Streamlit) + MCP server
pytest                            # run the test suite
```

### Making a change

1. **Open an issue first** for anything non-trivial (new backend, schema change,
   CLI surface) so we can agree on the shape — or bring it to Discord.
2. Branch off `main` (`feat/…`, `fix/…`, `docs/…`).
3. Add tests under `tests/` for behaviour changes, and run `pytest`.
4. If you touched a brain in `examples/`, run `open-index validate --brain examples/<name>`
   so schemas and entities stay consistent.
5. Update the README / `entity-management.md` when you change user-facing behaviour.
6. Open a PR describing *what* changed and *why*, and link the issue.

### Good first contributions

- A new example brain under [`examples/`](./examples) for a domain we don't cover yet.
- A connector in `connectors/` that pulls entities from an MCP server you use.
- Doc_type schemas for a common vertical (support, infra, sales, lending, personal).
- Sharper docs — if something tripped you up while onboarding, that's a bug.

Questions, ideas, or just want to show what you built? → **https://discord.gg/AQ3tusPtZn**
