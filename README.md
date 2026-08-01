### Droid Brain

The Droid Brain helps you build and maintain accurate organisational information in form of structured data to assist agents to operate in a complex environment effectively.

## Quick Start

```bash
# Option 1: Docker Compose (recommended)
docker compose up          # starts OpenSearch + seeds demo brain + launches Streamlit on :8501

# Option 2: Local dev
pip install --break-system-packages -e .
droid-brain seed-demo                                    # seed with sample infrastructure data
streamlit run app.py                                     # UI at http://localhost:8501
droid-brain mcp-server --transport stdio                 # MCP server for Claude, Cursor, etc.
```

### CLI
```bash
droid-brain list-brains
droid-brain structure demo              # 3 doc types, 11 entities, counts and examples
droid-brain search demo gateway         # full-text search across all entities
droid-brain get-entity demo <entity_id> # fetch a single entity
```

### MCP Server
```bash
droid-brain mcp-server --transport stdio                 # for Claude Desktop / Cursor
droid-brain mcp-server --transport sse --port 8000       # HTTP-based transport
```
Three tools exposed: `brain_structure`, `search_brain`, `fetch_entity`.

## Concepts

The primitives of creating a brain are as follows:
- **doc_types**: the different types of docs that you want to generate and maintain. This can be considered the equivalent of concepts you want to maintain in your knowledge.
- **doc_schema**: the schema of information stored for a given doc_type
- **entity**: an instance of a given doc_type, stored as per the schema is an entity
- **connectors**: the origin from where you extract the information (in case this is extracted from a given source)

Sample doc_types by domain:
- **infrastructure brain**: services, dashboards, runbooks, alert_definitions, releases
- **sales brain**: leads, deals, accounts, opportunities, meetings
- **lending brain**: loans, borrowers, brokers, applications

Read more about entity management in [Entity Management](./entity-management.md).

You can create instances of doc_types either manually or programmatically:
1. Through manual entry/upload from UI (Streamlit)
2. By talking to an agent that's connected to the brain via its CLI / MCP server
3. Through a webhook/API trigger from a script on your end (planned)
4. Through a recurring cron defined in the brain configuration (planned)

## Advanced Capabilities

Once you've setup the brain and it's functionally working, here's where the fun begins:

### Schema Design
The fields you define within the schema:
- The type of data in that field (string, number, boolean, etc.)
- The processing type of that field (keyword, timestamp, text, etc.)
- The kind of search you want to enable on top of that (semantic, syntactic)

### Search Design
- You can control the fields within the search tool that the agent has access to.
    - The fields that can be searched on
    - The fields that can be filtered on
    - The fields that can be sorted on
- Entity & Field Boosters — give different weights to different kinds of entities:
    - Field based boosters
    - Type based boosters
    - Temporal boosters