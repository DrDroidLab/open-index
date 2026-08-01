### Droid Brain


The Droid Brain helps you build and maintain accurate organisational information in form of structured data to assist agents to operate in a complex environment effectively.


The primitives of creating a brain are as follows:
- doc_types: the different types of docs that you want to generate and maintain. This can be considered the equivalent of concepts you want to maintain in your knowledge.
- doc_schema: the schema of information stored for a given doc_type
- entity: an instance of a given doc_type, stored as per the schema is an entity
- connectors: the origin from where you extract the information (in case this is extracted from a given source)


# Getting Started

### Creating your first brain
The way the project works is that you run one command and spin up an instance of the droid brain. There is nothing else to run: no OpenSearch, no Docker — every brain is a single local file (`~/.droid_brains/<name>.db`, SQLite with FTS5 full-text search built in).

```bash
pip install "droid-brain @ git+https://github.com/DrDroidLab/droid-brain"
droid-brain
```

Or as a true one-shot with [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/DrDroidLab/droid-brain droid-brain
```

On launch, the UI opens your most recent brain — or the create screen if you have none. You give your brain a name (this is its index), and you can seed it with demo entities to see search and boosting immediately:

```bash
droid-brain new acme-infra --seed-demo   # create a brain with demo entities
droid-brain ui acme-infra                # open it in the UI
```

### Creating your first doc_type
Then you create the first few types of doc_types (from the Doc Types tab in the UI). Sample doc types:
- software infrastructure brain: dashboards, metrics, panels, services, products, alert_definitions, runbooks, skills, releases, etc.
- sales brain: leads, deals, accounts, opportunities, meetings, etc.
- lending brain: loans, borrowers, brokers, applications, etc.

### Creating instances of doc_types
You can create instances of doc_types either manually or programmatically:
1. Through manual entry/upload from UI
2. By talking to an agent that's connected to the brain via it's CLI / MCP server
3. Through a webhook/API trigger from a script on your end
4. Through a recurring cron defined in the brain configuration

Read more about this in [Entity Management](./entity-management.md)

# Enabling your LLM/agent to use your brain:

### MCP Server
Run this command to get an MCP server up and running for your brain:

```bash
droid-brain mcp acme-infra
```

Or point any MCP client (Claude Desktop, Cursor, Claude Code, ...) at your brain via its config:

```json
{
  "mcpServers": {
    "droid-brain-acme-infra": {
      "command": "droid-brain",
      "args": ["mcp", "acme-infra"]
    }
  }
}
```

This MCP server has multiple tools for the agent to query the brain. Primarily, these three:
1. Brain structure: Gives a textual explanation of the data that's stored within the brain. What doc_types, how many instances of each, example values, description of the doc_type, etc.
2. Search the brain // apply filters
3. Fetch a specific entity

It also exposes a `create_entity` tool so the agent can add knowledge back into the brain.

### CLI
The same data can be queried using the CLI as well:

```bash
droid-brain list                            # all brains
droid-brain search acme-infra "payments"    # boosted full-text search
```

# Advanced Capabilities

Once you've setup the brain and it's functionally working, here's where the fun begins:

## Controlling the brain:
There are two parts to how you control the brain:
1. Schema Design
2. Search Design

Schema:
The fields you define within the schema.
- The type of data in that field (string, number, boolean, etc.)
- The processing type of that field (keyword, timestamp, text, etc.)
- The kind of search you want to enable on top of that (semantic, syntactic)

Search:
- You can control the fields within the search tool that the agent has access to.
    - The fields that can be searched on
    - The fields that can be filtered on
    - The fields that can be sorted on
- Entity & Field Boosters - this can help you to give different priorities and weights to different kind of entities within your organisation.
    - Field based boosters
    - Type based boosters
    - Temporal boosters

Field and type boosters work out of the box without any search server: the embedded engine ranks with bm25 column weights (a match on an entity's name counts far more than one in its content) multiplied by each doc_type's `boost` (set when creating the doc_type — e.g. give `service` a higher boost than `runbook`).