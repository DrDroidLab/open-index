### Open Index

[![tests](https://github.com/DrDroidLab/open-index/actions/workflows/tests.yml/badge.svg)](https://github.com/DrDroidLab/open-index/actions/workflows/tests.yml)
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

Prefer containers, or need a brain several agents share? →
**[`docs/deployment.md`](./docs/deployment.md)** (`docker compose --profile sqlite up`).

### Commands

| Command | What it does |
|---|---|
| `open-index init <name> [dir]` | Scaffold a new brain directory. |
| `open-index add-doc-type <name>` | Add a doc_type schema stub under `doc_types/`. |
| `open-index add-entity <file>` | Validate + store an entity JSON file. |
| `open-index import <file>` | Bulk-import entities from JSON / JSONL / CSV. |
| `open-index index` | (Re)load `entities/**/*.json` into the search index. |
| `open-index validate` | Validate `brain.yaml`, schemas, and every entity file (use in CI). |
| `open-index ingest <connector>` | Run a connector now to pull entities from an MCP server. |
| `open-index run [--force] [--loop N]` | Run every connector whose `schedule` is due (wire into cron/CI). |
| `open-index search <query> [-t doc_type]` | Search from the terminal. |
| `open-index ui` | Launch the Streamlit explorer (Explore / **Map** / Analytics / Jobs). |
| `open-index mcp [--read-only]` | Run the MCP context layer over stdio. **Read+write by default**; `--read-only` opts out of writes. |
| `open-index serve [--port --token --read-only]` | Serve the same read+write MCP context layer over **HTTP** for remote agents (bearer-token auth). |
| `open-index mcp-config [--url --token]` | Print the MCP connection block to paste into your agent. |

### A context layer for domain-specialized agents

Open Index is designed to sit behind agents specialized for a domain—legal,
marketing, customer support, sales, infrastructure, or a domain of your own. The
MCP server gives those agents structured context and a validated way to keep that
context current:

- **agent prompt** — dynamic domain navigation is published through MCP server
  instructions so supporting hosts can inject it before the first turn
- **read** — `navigation_guidelines()` refreshes those instructions;
  `search_brain()` and `get_entity()` retrieve domain context
- **write** — `put_entity()` (add/update an entity), `create_doc_type()` (define a concept)

Read and write is the default MCP mode so a domain agent can both use knowledge
and maintain it. Add `--read-only` when the agent should consume context without
mutating it. Claude Code is supported as one optional MCP client; `open-index init`
scaffolds `.mcp.json`, `CLAUDE.md`, and an editing skill as conveniences for it.

### Portable agent setup skill

[`skills/setup-open-index/SKILL.md`](./skills/setup-open-index/SKILL.md) follows the
portable Agent Skills `SKILL.md` format used by agent runtimes including OpenClaw,
Hermes, and Claude Code. Give or install this skill in the selected runtime when
the agent should set up Open Index itself. It covers installation, domain-brain
initialization, generic MCP wiring, default read/write verification, the
`--read-only` opt-out, and production guardrails.

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
2. **Bulk** — import a file directly, or let an agent write a batch in one call with
   `put_entities`:

   ```bash
   open-index import issues.csv --doc-type issue --asserted-by import:jira
   open-index import export.jsonl --dry-run        # validate first, write nothing
   ```

   JSON arrays, JSONL, and CSV all work. Bare slugs are qualified (`checkout` →
   `product:checkout`), CSV scalars are coerced, and a `related_to` column takes
   `target|meaning` pairs separated by `;`. A bad row is reported and skipped —
   the rest still land. `--asserted-by` / `--confidence` attribute the whole
   batch once instead of per row.
3. **Connectors** — `connectors/*.py` pull from an MCP server on a `schedule`; run with
   `open-index ingest <name>` or `open-index run` (cron/CI-friendly).
4. **Agent write-back** — a Stop hook that records learnings via `put_entity` (the
   "continuously improving" loop).

See [Entity Management](./entity-management.md) for guidance on cadence and decay.

### 4. Explore

`open-index ui` opens a read-only explorer. The sidebar always shows every doc_type
with its count and storage policy, so the structure is visible without navigating
anywhere. Four tabs: **Explore** (search + browse + drill into an entity's
relationships), **Map** (auto-anchored on the most-connected entities — click any
node to expand it), **Analytics** (what context CLI/MCP/UI clients fetched, and how
often — zero-result searches show what to model next), and **Jobs** (connectors and
their schedules).

# Using Open Index as your agent's context layer

`open-index mcp` runs an MCP server (stdio) exposing the brain to any MCP client —
**read and write by default**:

- The server publishes dynamic, brain-specific instructions as part of the agent
  prompt so supporting hosts can navigate the domain before the first tool call.
- `navigation_guidelines()` — refresh that guide after the index/schema changes.
- `search_brain(query, doc_types, limit)` · `get_entity(id)` — read.
- `put_entity(...)` · `put_entities([...])` · `create_doc_type(...)` — write (validated,
  honors the storage policy). `put_entities` writes a whole batch in one call and
  takes a shared `provenance` block.

Use `open-index mcp --read-only` (or `open-index serve --read-only`) to opt out
when an agent should retrieve domain context but never maintain it.

### Local context-fetch analytics

CLI and MCP searches, entity fetches, and navigation-guide reads are recorded in
the user's local state directory (`~/.local/state/open-index/`), outside the brain
checkout. The Analytics tab shows fetch counts
by client/operation, frequently fetched queries or entity IDs, latency, failures,
zero-result searches, and recent activity. This file stays local and is never
sent to Open Index's creators.

`open-index init` also includes optional Claude Code conveniences: `.mcp.json`, a
`CLAUDE.md` describing durable editing workflows (not runtime navigation), and an
**`edit-brain` skill**. They are one client integration, not a requirement for
building legal, marketing, support, or other specialized agents on Open Index.

## Using the brain from a cloud agent (production)

📖 **[Full deployment guide → `docs/deployment.md`](./docs/deployment.md)** — local,
remote (with and without Docker), TLS/proxying, and exactly what to paste into
Claude Code, Claude Desktop, or Cursor.

### Getting the connection details

Never hand-assemble the config. Ask for it:

```bash
open-index mcp-config --brain ./my-brain                        # local (stdio)
open-index mcp-config --url brain.acme.internal:8080 --token $OPEN_INDEX_TOKEN
open-index mcp-config --url https://brain.acme.com --token $TOKEN --cli   # `claude mcp add …`
open-index mcp-config --brain ./my-brain > .mcp.json            # pipes where it belongs
```

`open-index serve` prints the same details on startup — including the addresses a
*remote* client can actually reach. (The bind address it listens on, `0.0.0.0`, is
not one of them.) Behind a proxy or tunnel, pass `--public-url` so what's printed
is what agents should use.

### With Docker (recommended for a shared brain)

```bash
cp .env.example .env          # set OPEN_INDEX_TOKEN + BRAIN_DIR

docker compose --profile sqlite     up --build    # single writer, no extra services
docker compose --profile opensearch up --build    # many writers, incl. the cluster
```

Both serve `http://localhost:8080/mcp`. Your `brain.yaml` is identical either way —
the profile sets `OPEN_INDEX_SEARCH_BACKEND`, which overrides the file. Add
`--profile ui` for the explorer on `:8501`. The brain directory is mounted, not
baked into the image, so doc_types and entities stay in git.

### Without Docker

```bash
pip install 'open-index[serve]'                  # add ,opensearch for that backend
open-index index --brain /srv/acme-brain         # load file-backed entities first
OPEN_INDEX_TOKEN=… open-index serve --brain /srv/acme-brain --port 8080
```

`serve` exposes the same read+write tools over **streamable HTTP** with
**bearer-token auth**. Without a token the endpoint is unauthenticated — anyone who
can reach the port can write to your brain. Use `--read-only` for a queryable
endpoint that agents can't mutate.

### Choosing a backend

**SQLite is single-writer.** That, not entity count, is the line: the moment a
second agent needs to write, move to OpenSearch. It also gives native per-field
boosting, **fuzzy** (typo-tolerant) search, and k-NN semantic search that scales
past SQLite's ~10k-entity brute-force ceiling.

Select it per-environment without touching `brain.yaml`:

```bash
export OPEN_INDEX_SEARCH_BACKEND=opensearch
export OPEN_INDEX_OPENSEARCH_HOSTS=https://opensearch.internal:9200
```

…or commit it, with secrets as `${ENV}` refs resolved at connect time:

```yaml
search:
  backend: opensearch
  hosts: ["https://opensearch:9200"]
  index: open_index_acme          # optional; defaults to open_index_<name>
  username: "${OPENSEARCH_USER}"
  password: "${OPENSEARCH_PASSWORD}"
  use_ssl: true
  verify_certs: true
```

The doc_type/file-entity part comes from git; index-backed data lives only in the
cluster (or `brain.db`), so give it a persistent home and a backup. Rule of thumb:
**local/dev → SQLite; shared endpoint → OpenSearch + `serve`.**

# Controlling search

📖 **[Full configuration reference → `docs/configuration.md`](./docs/configuration.md)** —
decision tables for `storage: file | index`, SQLite vs OpenSearch, and every search knob.

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

Note that `pytest` **skips** the MCP and UI suites when those extras aren't
installed, so a green run on a partial install doesn't mean much — use `[all]`
(or at least `[ui,mcp]`) locally. CI installs them explicitly and fails if they
are missing.

### Making a change

1. **Open an issue first** for anything non-trivial (new backend, schema change,
   CLI surface) so we can agree on the shape — or bring it to Discord.
2. Branch off `main` (`feat/…`, `fix/…`, `docs/…`).
3. Add tests under `tests/` for behaviour changes, and run `pytest`.
4. If you touched a brain in `examples/`, run `open-index validate --brain examples/<name>`
   so schemas and entities stay consistent.
5. Update the README / `entity-management.md` when you change user-facing behaviour.
6. Open a PR describing *what* changed and *why*, and link the issue. CI runs the
   test suite on Python 3.10 and 3.13 and validates every brain in `examples/`.

### Good first contributions

- A new example brain under [`examples/`](./examples) for a domain we don't cover yet.
- A connector in `connectors/` that pulls entities from an MCP server you use.
- Doc_type schemas for a common vertical (support, infra, sales, lending, personal).
- Sharper docs — if something tripped you up while onboarding, that's a bug.

Questions, ideas, or just want to show what you built? → **https://discord.gg/AQ3tusPtZn**
