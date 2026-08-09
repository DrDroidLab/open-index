# Many indexes on one host

Serving several independent brains from one machine, each with its own MCP
endpoint and its own explorer UI:

```
https://<lb-host>/support/mcp     https://<lb-host>/support/ui
https://<lb-host>/sales/mcp       https://<lb-host>/sales/ui
https://<lb-host>/policies/mcp    https://<lb-host>/policies/ui
```

Each index is a separate brain — its own doc_types, entities, credentials and
read/write policy. They share one OpenSearch cluster and one nginx.

Isolation is enforced at both doors: an index's bearer token opens only its own
`/mcp`, and its UI password opens only its own `/ui`.

---

## 1. Do I need one Docker setup per index?

No. One setup, several containers.

`open-index serve` serves exactly one brain per process, so each index does need
its own container. But the expensive part is the **search cluster**, and that is
shared: the OpenSearch index name is derived per brain
(`OPEN_INDEX_OPENSEARCH_INDEX`), so many brains live in one cluster without
seeing each other's data.

Measured on a 2-vCPU / 3.8GB VM running two indexes, each with MCP and UI:

| Component | Memory | Per what |
|---|---|---|
| OpenSearch | 983 MB | once, shared |
| nginx | 3 MB | once, shared |
| brain container (MCP) | ~255 MB | **per index** |
| ui container | ~71 MB | **per index** |

So the first index costs ~1.3GB and each additional one ~326MB. On a 3.8GB box
that is roughly 4–6 indexes with UIs, or 6–8 without.

Two ways to fit more: set `ui: false` on indexes nobody browses, or move
embeddings to an API (`OPEN_INDEX_EMBEDDING_*`), which drops the local model
from every container. Note the UI figure grows once someone runs a semantic
search there — the model loads lazily.

```
            ┌──────────────── nginx :80 ────────────────┐
            │  /support/mcp  →  brain-support  (token)  │──▶ ┐
  LB  ──▶   │  /support/ui   →  ui-support     (basic)  │──▶ │
            │  /sales/mcp    →  brain-sales    (token)  │──▶ ├─▶ OpenSearch
            │  /sales/ui     →  ui-sales       (basic)  │──▶ ┘   one index each
            └───────────────────────────────────────────┘
```

---

## 2. Set it up

Everything is generated from one file, `deploy/fleet/indexes.yml`:

```yaml
public_base_url: https://brain.acme.com   # what agents will connect to
brains_root: /home/azureuser/brains       # one subdirectory per index
http_port: 80
opensearch_heap: 512m

indexes:
  - name: support
    description: Customer support knowledge.
  - name: sales
    description: Sales pipeline.
  - name: policies
    description: Reference only.
    read_only: true        # search/read tools, no writes
    ui: false              # no explorer for this one (saves ~71MB)
```

Then:

```bash
cd deploy/fleet
./fleet.py up
```

That builds the image, creates any missing brain directories (with the
permissions the container needs), generates a per-index bearer token, renders
the compose and nginx config, and starts everything. It is idempotent — run it
again after any edit.

**Adding an index later** is one command:

```bash
./fleet.py add marketing --description "Campaigns and positioning"
```

Existing indexes keep running; only the new container starts and nginx reloads.

Other commands: `./fleet.py tokens` (connection details), `status`, `logs
<name>`, `render` (write config without starting).

> `indexes.yml` is the source of truth. `docker-compose.generated.yml` and
> `nginx/default.conf` are regenerated on every run — edit the former, never the
> latter two.

---

## 3. Put it behind a load balancer

nginx publishes on `http_port` (80 by default). Point the LB at that.

**Health check:** `GET /healthz` → `200 ok`. Unauthenticated and free of brain
detail, so it is safe as an LB probe.

**Directory:** `GET /` returns JSON listing every index and its URL. Names only —
no tokens.

**Terminate TLS at the LB.** The brains speak plain HTTP. Once TLS is on, set
`public_base_url: https://...` and re-run `./fleet.py up` so each brain
advertises the right URL.

**Forward the `Host` header.** open-index validates it (DNS-rebinding
protection) and accepts loopback plus whatever `public_base_url` names. If your
LB rewrites Host to something else, add it:

```bash
open-index serve --allowed-host lb.internal --allowed-host brain.acme.com
# or OPEN_INDEX_ALLOWED_HOSTS=lb.internal,brain.acme.com
```

A `421 Invalid Host header` means exactly this — the Host that arrived is not on
the list. `--allowed-host '*'` disables the check, which is only reasonable when
the proxy in front already validates Host.

**Don't buffer responses.** Streamable HTTP keeps responses open; a buffering LB
will appear to hang. The bundled nginx sets `proxy_buffering off` — configure the
same on the LB, with an idle timeout of at least a few minutes.

---

## 4. Access an index

Each index has its own token, so a client holding the `support` token cannot
read or write `sales`.

```bash
cd deploy/fleet && ./fleet.py tokens
```

Point an agent at one:

```bash
source deploy/fleet/.env
open-index mcp-config --url https://brain.acme.com/support/mcp \
  --token $OPEN_INDEX_TOKEN_SUPPORT > .mcp.json
```

which writes:

```json
{
  "mcpServers": {
    "open-index": {
      "type": "http",
      "url": "https://brain.acme.com/support/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Registering several indexes in one agent — give each a distinct server name so
the agent can tell them apart:

```bash
open-index mcp-config --url https://brain.acme.com/support/mcp --token $OPEN_INDEX_TOKEN_SUPPORT --name support
open-index mcp-config --url https://brain.acme.com/sales/mcp   --token $OPEN_INDEX_TOKEN_SALES   --name sales
```

Check an endpoint by hand:

```bash
curl -i -X POST https://brain.acme.com/support/mcp \
  -H "Authorization: Bearer $OPEN_INDEX_TOKEN_SUPPORT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

`401` = token missing or wrong. `404` = wrong path (the index name is
case-sensitive). `421` = Host header not allowed, see above.

### The explorer UI

Each index also gets a read-only explorer at `/<name>/ui`, behind HTTP basic
auth — username is the index name, password from `./fleet.py tokens`:

```
https://brain.acme.com/support/ui     support / <ui password>
```

The password exists because **Streamlit has no authentication of its own**.
Published without a gate, anyone who could reach the host could read the entire
brain. Each index has its own credential file, so the `sales` password does not
open the `support` UI.

The UI is deliberately read-only — writes go through MCP or the CLI so every
change is validated. Turn it off per index with `ui: false`.

If the page loads but hangs "connecting", the proxy in front is not forwarding
websockets: the LB needs the `Upgrade`/`Connection` headers and buffering off,
the same as nginx already does.

---

## 5. Populate an index

Everything below targets one index. Substitute the index name.

### Bulk import a spreadsheet

Export the sheet to CSV, drop it where the container can see it (the brain
directory is mounted), then:

```bash
cd deploy/fleet
D="docker compose -f docker-compose.generated.yml --env-file .env"

cp issues.csv /home/azureuser/brains/support/
$D exec -T brain-support open-index import /brain/issues.csv \
    --brain /brain --doc-type issue \
    --asserted-by import:2026-08-crm --confidence 0.8
```

Check first with `--dry-run`, which validates and writes nothing.

Notes that save time:

- **Define the doc_type before importing.** `open-index add-doc-type issue
  --brain /brain --storage file`, then edit
  `doc_types/issue.yaml` to describe the columns. Import fails per-row on an
  unknown doc_type.
- **Bare ids are qualified**: an `id` column containing `checkout` becomes
  `issue:checkout`.
- **Relationships come from a `related_to` column**, semicolon-separated, each
  `target|meaning`:
  `product:checkout|is a common issue of; team:payments|owned by`.
  Those edges are what make this a graph rather than a table — a spreadsheet
  imported without them is just rows.
- **Bad rows are skipped, not fatal.** The command reports each one and exits
  non-zero, so CI notices, but the good rows still land.
- `--asserted-by` / `--confidence` attribute the whole batch once, so later
  readers can tell imported rows from agent-inferred ones.

### Let an agent write to it

Point the agent at that index's URL (section 4) and ask. It calls
`navigation_guidelines` first — the guide is injected into the MCP handshake, so
it knows your doc_types and relationship vocabulary before its first turn. Use
`put_entities` for batches rather than one call per row.

### Pull from another system on a schedule

Add a connector under `<brain>/connectors/*.py` and run
`open-index ingest <name>` (or `open-index run` from cron). See
[deployment.md](./deployment.md#6-the-other-direction-connectors-that-pull-from-an-mcp-server).

### After a bulk change

```bash
$D exec -T brain-support open-index validate --brain /brain
$D exec -T brain-support open-index index --brain /brain
```

`validate` catches malformed ids and wrong relationship targets. `index`
reconciles file-backed entities from disk.

---

## 6. Operations

**Back up** two things per index: the brain directory (doc_types + file-backed
entities — usually git) and the OpenSearch data volume, which is the *only* home
for `storage: index` entities. Losing the volume means re-ingesting them.

**Logs:** `./fleet.py logs support`, or `docker compose ... logs -f`.

**Restart safety:** every container is `restart: unless-stopped`, so the stack
comes back after a reboot. The embedding model is cached in a named volume, so a
restart doesn't re-download it.

**Removing an index:** delete its entry from `indexes.yml`, run `./fleet.py up`
(the container is removed via `--remove-orphans`), then delete the brain
directory and the cluster index if you want the data gone:

```bash
curl -X DELETE http://127.0.0.1:9200/open_index_support
```

**Rotating a token:** delete that index's line from `deploy/fleet/.env` and run
`./fleet.py up` — a new token is generated. Clients must be updated.

**Capacity:** watch `docker stats`. If OpenSearch starts evicting or the box
swaps, either raise `opensearch_heap` and the VM size together, or move
embeddings to an API so each brain container drops to well under 100MB.
