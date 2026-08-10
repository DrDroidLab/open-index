# Deploying a brain and connecting your agent

This guide answers three questions, in order:

1. [Which setup do I want?](#1-which-setup-do-i-want) — local vs remote, SQLite vs OpenSearch
2. How do I run it — [local](#2-local-brain-stdio), [remote without Docker](#3-remote-brain-without-docker), [remote with Docker](#4-remote-brain-with-docker)
3. [How do I get the MCP details into my agent?](#5-connecting-your-agent) — the part that is easy to get wrong

There is a second, unrelated use of "MCP" in open-index — *connectors*, which pull
data **from** someone else's MCP server into your brain. That's [section 6](#6-the-other-direction-connectors-that-pull-from-an-mcp-server).

---

## 1. Which setup do I want?

Two independent choices. Pick one from each column.

**Where does the brain run?**

| | Local (stdio) | Remote (HTTP) |
|---|---|---|
| How the agent connects | It launches `open-index mcp` itself | It connects to a URL you host |
| Needs a URL or token | No | Yes |
| Who can use it | One agent, on your machine | Any agent that can reach the host |
| Use when | You're the only user; the brain lives in a repo you have checked out | A team, cloud agents, or CI shares one brain |

**Which search backend?**

| | SQLite (default) | OpenSearch |
|---|---|---|
| External services | None — one `brain.db` file | An OpenSearch cluster |
| Concurrent writers | **One.** SQLite is single-writer | Many |
| Semantic search | Brute-force cosine scan, fine to ~10k entities | Native k-NN, scales past that |
| Extras | Native per-field boosting, fuzzy/typo-tolerant search | |
| Use when | Local brains, single agent, trials, read-mostly | Several agents writing, or >10k entities |

**Rule of thumb:** local + SQLite to start. Move to remote + OpenSearch when a
*second writer* appears — that's the line SQLite can't cross, not entity count.

> **OpenSearch is only set up for you in the Docker path.** Running it outside
> Docker means operating a cluster yourself; open-index will happily connect to
> one ([config below](#opensearch-without-docker)), but this repo only ships a
> ready-made cluster in `docker-compose.yml`.

Switching backends does **not** require editing `brain.yaml` — set
`OPEN_INDEX_SEARCH_BACKEND=sqlite|opensearch` in the environment and it wins over
the file. That's what the compose profiles do.

---

## 2. Local brain (stdio)

Nothing to host. The agent starts the server itself over stdio.

```bash
pip install -e '.[all]'
open-index init my-brain
open-index index --brain my-brain
```

`open-index init` already writes a `.mcp.json` into the brain directory, so if
you open Claude Code **in that folder**, it connects automatically — no further
setup.

To connect from somewhere else (a different repo, Claude Desktop, Cursor), get
the config block:

```bash
open-index mcp-config --brain ./my-brain
```

```json
{
  "mcpServers": {
    "open-index": {
      "command": "open-index",
      "args": ["mcp", "--brain", "/absolute/path/to/my-brain"]
    }
  }
}
```

The path is absolutized deliberately: your agent's working directory is usually
not the brain directory, and a relative `--brain .` quietly opens the wrong place
(or an empty one). See [section 5](#5-connecting-your-agent) for where this block goes.

---

## 3. Remote brain, without Docker

Use this when you have a VM and don't want containers. The brain becomes an HTTP
MCP endpoint that any agent can register by URL.

### Install and run

```bash
# On the host
pip install 'open-index[serve]'                 # add ,opensearch if using OpenSearch
git clone <your-brain-repo> /srv/acme-brain     # your doc_types + entities
open-index index --brain /srv/acme-brain        # load file-backed entities

export OPEN_INDEX_TOKEN=$(openssl rand -hex 32)
open-index serve --brain /srv/acme-brain --port 8080
```

`serve` prints exactly what to connect to:

```
open-index · brain 'acme' · read+write · search backend: sqlite
  listening on 0.0.0.0:8080

  on this machine        http://127.0.0.1:8080/mcp
  from another machine   http://10.0.1.42:8080/mcp

  auth: Authorization: Bearer 9f3a…******
  agent config:  open-index mcp-config --url http://10.0.1.42:8080 --token $OPEN_INDEX_TOKEN
```

> **`0.0.0.0` is not an address.** It's the *bind* address — "listen on every
> interface". It is never what you paste into an agent. That's why the banner
> prints the reachable addresses separately.

### The token is not optional

`serve` exposes `put_entity` and `create_doc_type`. Without a token, anyone who
can reach the port can rewrite your brain. Either set `OPEN_INDEX_TOKEN`, or pass
`--read-only` to drop the write tools:

```bash
open-index serve --brain /srv/acme-brain --read-only          # queryable, not writable
```

A common shape is two endpoints: a read-only one on an open port, and an
authenticated read+write one for the agents allowed to author.

### Keep it running (systemd)

```ini
# /etc/systemd/system/open-index.service
[Unit]
Description=open-index brain (MCP)
After=network-online.target

[Service]
User=openindex
WorkingDirectory=/srv/acme-brain
Environment=OPEN_INDEX_TOKEN=<token>
# Uncomment to use an OpenSearch cluster instead of SQLite:
# Environment=OPEN_INDEX_SEARCH_BACKEND=opensearch
# Environment=OPEN_INDEX_OPENSEARCH_HOSTS=https://opensearch.internal:9200
ExecStartPre=/usr/local/bin/open-index index --brain /srv/acme-brain
ExecStart=/usr/local/bin/open-index serve --brain /srv/acme-brain --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`ExecStartPre` matters: file-backed entities live in git, not in the index. After
a `git pull` or a fresh machine, the index is empty until `open-index index` runs
and the brain answers every query with nothing.

### TLS / behind a proxy

`serve` speaks plain HTTP. For TLS, terminate at nginx/Caddy and forward to it.
Streamable HTTP uses long-lived responses, so disable response buffering:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8080/mcp;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Authorization $http_authorization;
    proxy_buffering off;          # required — SSE/streaming responses
    proxy_read_timeout 3600s;
}
```

Then tell `serve` its public name so the banner and `mcp-config` print the URL
agents should actually use, rather than the internal one:

```bash
open-index serve --brain /srv/acme-brain --public-url https://brain.acme.com/mcp
```

### OpenSearch without Docker

Point `brain.yaml` at your existing cluster. Secrets stay as `${ENV}` refs,
resolved when the connection is opened:

```yaml
search:
  backend: opensearch
  hosts: ["https://opensearch.internal:9200"]
  index: open_index_acme          # optional; defaults to open_index_<brain name>
  username: "${OPENSEARCH_USER}"
  password: "${OPENSEARCH_PASSWORD}"
  use_ssl: true
  verify_certs: true
```

Or leave `brain.yaml` on SQLite and override per-environment:

```bash
export OPEN_INDEX_SEARCH_BACKEND=opensearch
export OPEN_INDEX_OPENSEARCH_HOSTS=https://opensearch.internal:9200
```

---

## 4. Remote brain, with Docker

The shortest path to a shared brain, and the only path where OpenSearch is set up
for you.

```bash
cp .env.example .env
# Set OPEN_INDEX_TOKEN (openssl rand -hex 32) and BRAIN_DIR (path to your brain).
```

**SQLite** — no external services:

```bash
docker compose --profile sqlite up --build
```

**OpenSearch** — adds a single-node cluster with a persistent volume:

```bash
docker compose --profile opensearch up --build
```

Both serve `http://localhost:8080/mcp`. The only difference is
`OPEN_INDEX_SEARCH_BACKEND`; your `brain.yaml` is identical either way, so you can
switch by changing the profile and nothing else.

Add the explorer UI on `:8501` alongside either:

```bash
docker compose --profile opensearch --profile ui up
```

### What the container does on start

The brain directory is **mounted, not baked in** (`BRAIN_DIR:/brain`) — doc_types
and entities stay in your git repo. On start the entrypoint:

1. fails fast with a clear message if `/brain/brain.yaml` isn't there,
2. waits for OpenSearch to report healthy when that backend is selected,
3. runs `open-index index` so file-backed entities are loaded (skip with
   `OPEN_INDEX_SKIP_INDEX=1`),
4. execs `open-index serve`.

### Permissions on the mounted brain directory

The container runs as uid **10001** (not root), so a bind-mounted brain directory
owned by your user is not writable by it — indexing fails on the first write.
Give the container ownership and keep group access for yourself:

```bash
sudo chown -R 10001:"$(id -g)" /path/to/my-brain
chmod -R g+rwX /path/to/my-brain
```

You can still read and edit the files; writes from inside the container land as
uid 10001 with your group.

### Many brains from one process

`serve --brain <dir>` runs one brain. For more than a handful, `--brains
<root>` serves every brain under a directory from a single process, each at
`/<name>/mcp`:

```bash
open-index serve --brains /srv/brains --port 8080
#   /srv/brains/support/  →  /support/mcp
#   /srv/brains/sales/    →  /sales/mcp
```

This matters because of what is *not* duplicated. A process per brain re-loads
the Python runtime and a ~250MB resident embedding model each time, so a modest
host tops out at a handful. In one process the model is loaded once and a brain
costs only its config and doc_types.

Measured on the bundled example brain, SQLite-backed:

| Brains | One process | One process per brain |
|---|---|---|
| 50 | 345 MB | ~13 GB |
| 200 | **373 MB** | ~53 GB |

That is **1.8MB of marginal cost per brain**, and 200 mount in ~4 seconds.

Each brain keeps its own storage, its own read/write policy and its own token —
`OPEN_INDEX_TOKEN_<NAME>` gates one brain (`OPEN_INDEX_TOKEN_SALES_EU` for
`sales-eu/`), and `--token` covers any without one. Nothing is shared between
brains except the process and the model.

Two extras come with it: `GET /` lists every brain with its URL, entity count
and doc_types, and `GET /healthz` is an unauthenticated probe for a load
balancer.

> **Prefer SQLite here.** With OpenSearch, every brain is a separate cluster
> index and therefore a shard; the working guidance is ~20 shards per GB of
> heap, so hundreds of brains would hit that ceiling long before RAM. SQLite
> gives each brain its own file and no shard cost at all. Use OpenSearch for the
> few brains that genuinely need concurrent writers or >10k entities.

### Running one-off commands

```bash
docker compose --profile sqlite run --rm brain-sqlite validate
docker compose --profile sqlite run --rm brain-sqlite search "checkout latency"
docker compose --profile sqlite run --rm brain-sqlite ingest my-connector
```

`--brain /brain` is added for you.

### Plain `docker run`, no compose

```bash
docker build -t open-index .
docker run -p 8080:8080 \
  -v "$PWD/my-brain:/brain" \
  -e OPEN_INDEX_TOKEN=secret \
  open-index serve
```

### Notes for real deployments

- **Persistence.** SQLite: `brain.db` is inside your mounted brain dir — back that
  up. OpenSearch: the `opensearch-data` named volume. Index-backed entities
  (`storage: index`) exist **only** there; they are not in git and are not
  recreated by `open-index index`. Back it up or be able to re-ingest.
- **The OpenSearch cluster here has security disabled** (`DISABLE_SECURITY_PLUGIN=true`)
  and binds to loopback. That's fine for a single host where only the brain
  container talks to it; enable the security plugin and set
  `search.username`/`password` before putting it on a shared network.
- **Behind a proxy**, set `OPEN_INDEX_PUBLIC_URL` in `.env` so the printed
  connection details are the ones agents can actually use.

---

## 5. Connecting your agent

You need two things: the **URL** (remote) or **brain path** (local), and the
**token** (remote only). `mcp-config` assembles both into the right block.

```bash
# Local brain
open-index mcp-config --brain ./my-brain

# Remote brain — host:port, or a full URL; /mcp is appended if you omit it
open-index mcp-config --url brain.acme.internal:8080 --token $OPEN_INDEX_TOKEN

# As a `claude mcp add` one-liner instead of JSON
open-index mcp-config --url https://brain.acme.com/mcp --token $TOKEN --cli
```

It writes to stdout, so it pipes straight where it belongs:

```bash
open-index mcp-config --brain ./my-brain > .mcp.json
```

### Where the block goes

| Client | Location |
|---|---|
| **Claude Code** (project) | `.mcp.json` in the repo root — shared with the team via git |
| **Claude Code** (user-wide) | `claude mcp add …` (use `--cli` to get the exact command) |
| **Claude Desktop** | `claude_desktop_config.json` — same `mcpServers` shape |
| **Cursor** | `.cursor/mcp.json` — same `mcpServers` shape |
| **Anything else** | Any MCP client that speaks stdio or streamable HTTP |

A remote block looks like this:

```json
{
  "mcpServers": {
    "open-index": {
      "type": "http",
      "url": "https://brain.acme.com/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

> Committing a token into `.mcp.json` puts it in git history. For a shared repo,
> prefer a per-developer user-scoped entry, or a read-only endpoint with no token
> for the committed config.

### Check it works before blaming the agent

```bash
# 401 = the server is up and your token is wrong or missing.
# 406/400 = you reached the MCP endpoint (it wants a proper MCP handshake). Good.
curl -i -X POST https://brain.acme.com/mcp \
  -H "Authorization: Bearer $OPEN_INDEX_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Once connected, the agent should call `navigation_guidelines()` first — it
reports the doc_types, fields, and relationship vocabulary of *your* brain.

### Troubleshooting

| Symptom | Cause |
|---|---|
| Agent can't connect at all | You used the bind address (`0.0.0.0`) instead of a reachable one. Re-read the `serve` banner. |
| `401 unauthorized` | Token missing/wrong. The header must be exactly `Authorization: Bearer <token>`. |
| Connects, but `404` | Missing the `/mcp` path. `mcp-config` appends it; hand-written URLs often don't. |
| Connects, but every search is empty | `open-index index` never ran on this host, so file-backed entities aren't loaded. |
| Tools are read-only unexpectedly | The server was started with `--read-only`. |
| Writes fail with "unknown doc_type" | Create the doc_type first (`create_doc_type`), or check `navigation_guidelines()` for existing ones. |
| Local stdio server opens an empty brain | Relative `--brain .` resolved against the agent's cwd. Use an absolute path — `mcp-config` emits one. |
| Hangs/timeouts behind nginx | Response buffering is on. Set `proxy_buffering off`. |

---

## 6. The other direction: connectors that pull *from* an MCP server

Everything above is about exposing **your brain** over MCP. A *connector* is the
reverse: a script in `<brain>/connectors/*.py` that calls **someone else's** MCP
server and turns the results into entities.

```python
from open_index.connectors import Connector, EntitySpec


class LinearConnector(Connector):
    name = "linear-issues"

    # Where the source MCP server lives. ${ENV} refs are resolved at run time,
    # so the URL and token stay out of git.
    mcp_url = "${LINEAR_MCP_URL}"
    mcp_auth_headers = {"Authorization": "Bearer ${LINEAR_TOKEN}"}

    schedule = "daily"          # manual | hourly | daily | weekly | 6h | 30m | 1w

    def extract_issues(self):
        for item in self.paginate("list_issues", result_key="issues"):
            yield EntitySpec(
                doc_type="issue",
                id=f"issue:{item['id']}",
                name=item["title"],
                fields={"status": item["state"]},
                related_to=[(f"product:{item['project']}", "belongs to product")],
            )
```

**Where do you get `mcp_url`?** From whoever runs that server:

- A hosted vendor MCP server — from their docs (e.g. `https://mcp.vendor.com/mcp`).
- Another open-index brain — its `open-index serve` URL, i.e. `http://host:8080/mcp`.
- A local stdio-only MCP server — **not supported here.** Connectors speak
  streamable HTTP/SSE over `httpx` only. It needs an HTTP endpoint.

The URL must be the full endpoint path, the same one an agent would use.

```bash
export LINEAR_MCP_URL=https://mcp.linear.app/mcp
export LINEAR_TOKEN=...

open-index list-connectors                  # what's discovered, and its URL
open-index ingest linear-issues             # run it now
open-index run                              # run everything whose schedule is due
open-index run --loop 3600                  # or wire `open-index run` into cron/CI
```

`open-index run` tracks last-run times in a gitignored `.open_index_state.json`,
so it decides *whether* a connector is due — your cron drives the clock.
