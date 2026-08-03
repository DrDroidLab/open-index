"""Templates for `droid-brain init` / `add-doc-type` — the minimal-friction path.

`init` drops a runnable brain (config + one example doc_type + one entity) so a
new user goes from zero to a queryable map with a single command.
"""

from __future__ import annotations

from pathlib import Path

BRAIN_YAML = """\
name: {name}
description: A context graph built with droid-brain.

storage:
  backend: sqlite
  path: ./brain.db

search:
  backend: sqlite   # pluggable: sqlite | opensearch
"""

EXAMPLE_DOC_TYPE = """\
doc_type: note
description: A free-form note or concept. Replace this with your own doc_types.
storage: file   # curated — this type ships JSON entities tracked in git
display:
  label_field: name
  color: "#6b7280"
schema:
  fields:
    - { name: name,        type: string, processing: keyword, search: syntactic, boost: 6 }
    - { name: description, type: text,   processing: text,    search: semantic }
"""

DOC_TYPE_TEMPLATE = """\
doc_type: {name}
description: TODO describe what a {name} is.
# storage: index = the search DB is the source of truth (connectors/agents write
# these; not tracked in git). Set 'file' to keep entities/{name}/*.json in git.
storage: index
display:
  label_field: name
  color: "{color}"
schema:
  fields:
    - {{ name: name,        type: string, processing: keyword, search: syntactic, boost: 6 }}
    - {{ name: description, type: text,   processing: text,    search: semantic }}
    # Add more fields here. `boost` raises a field's search weight.
# Declare the relationship kinds entities of this type use, so correlations are
# discoverable (and lightly validated) instead of improvised. Optional.
# relationships:
#   - {{ name: "depends on", target_doc_type: {name} }}
"""

EXAMPLE_ENTITY = """\
{
  "doc_type": "note",
  "id": "note:welcome",
  "name": "Welcome",
  "description": "Your first entity. Add more under entities/ and run `droid-brain index`.",
  "related_to": []
}
"""

# A palette to cycle through when scaffolding new doc_types.
_PALETTE = [
    "#7c3aed", "#0891b2", "#059669", "#d97706",
    "#dc2626", "#4f46e5", "#0284c7", "#db2777",
]


def color_for_index(i: int) -> str:
    return _PALETTE[i % len(_PALETTE)]


# .mcp.json — auto-connects the brain to Claude Code (and any MCP client) so an
# agent opened in this folder can read AND write the brain from second one.
MCP_JSON = """\
{
  "mcpServers": {
    "droid-brain": {
      "command": "droid-brain",
      "args": ["mcp", "--brain", "."]
    }
  }
}
"""

# CLAUDE.md — teaches the agent the model + workflows so you can just talk to it.
CLAUDE_MD = """\
# {name} — a droid-brain context graph

This repository is a **droid-brain**: a domain-agnostic context graph about our
organisation. You (the agent) can both **read** it (for context during work) and
**write** to it (to record new knowledge). It is also wired to you as an MCP
server named `droid-brain` — prefer those tools; the files here are the source of
truth behind them.

## Model
- **doc_type** — a concept we track (a schema). Lives in `doc_types/<name>.yaml`.
- **entity** — one instance of a doc_type. Lives in `entities/<doc_type>/<slug>.json`.
- **relationship** — every entity may link to others via `related_to`:
  `{{ "target": "<entity_id>", "relationship_edge_meaning": "<free text>" }}`.
  Ids look like `<doc_type>:<slug>` (e.g. `product:checkout`).

## MCP tools
- `navigation_guidelines()` — READ FIRST. What doc_types exist, fields, examples.
- `search_brain(query, doc_types, limit)` / `get_entity(id)` — query.
- `put_entity(doc_type, id, name, fields, related_to)` — add/update an entity.
- `create_doc_type(doc_type, description, fields, color)` — define a new concept.

## Editing this brain
When asked to add/update knowledge, follow the **edit-brain** skill
(`.claude/skills/edit-brain/SKILL.md`) — it covers defining doc_types, adding
entities, and correlating them.

## Storage policy (per doc_type)
Each doc_type declares where its entities live:
- `storage: index` (default) — the search DB is the source of truth; entities are
  NOT written to files. Use for connector-pulled, high-volume, or machine-generated
  data (they'd drown git).
- `storage: file` — JSON under `entities/<doc_type>/` is the source of truth,
  git-tracked. Use for curated, human/agent-authored entities worth versioning.

## Workflows
- **Define a doc_type:** write `doc_types/<name>.yaml` (see `doc_types/note.yaml`
  for the shape), or call `create_doc_type`. Reuse an existing type if one fits.
  Pick `storage: file` for curated types, `index` for generated ones.
- **Add entities:** write `entities/<doc_type>/<slug>.json`, or call `put_entity`.
  Always set meaningful `related_to` edges — the graph is the point.
- **Pull from a tool:** add a connector in `connectors/<name>.py` (see
  `connectors/example_connector.py`), then `droid-brain ingest <name>`.
- **After editing files by hand:** run `droid-brain index` then `droid-brain validate`.

## Keeping the brain improving
Record durable learnings as entities (e.g. a `memory` or `runbook` doc_type) via
`put_entity` — root causes, "this usually means X", decisions. See the example
reflection hook in `hooks/` to do this automatically after sessions.
"""

# An opt-in Stop hook: after a Claude Code session, distill learnings into the
# brain. Shipped as an example (not enabled) — the reflection command is yours to
# define; the point is to show where the write-back flywheel plugs in.
REFLECT_HOOK = """\
#!/usr/bin/env bash
# Example droid-brain reflection hook (Pattern A: agent writes back).
#
# Enable by adding to .claude/settings.json:
#   {{ "hooks": {{ "Stop": [ {{ "hooks": [ {{ "type": "command",
#      "command": "$CLAUDE_PROJECT_DIR/hooks/reflect.sh" }} ] }} ] }} }}
#
# This stub just reminds you. Replace the body with a real reflection step —
# e.g. invoke an agent that summarizes the session and calls the brain's MCP
# `put_entity` tool to store a `memory`/`runbook` entity. Keep it cheap; run it
# every few sessions, not every turn.
echo "[droid-brain] reflection hook fired — wire up put_entity to capture learnings." >&2
"""

EXAMPLE_SCHEDULED_CONNECTOR = '''\
"""Example scheduled connector.

Point `mcp_url` at a real MCP server, set `schedule`, then let it run via:
    droid-brain ingest example-connector      # run now
    droid-brain run                            # run if its schedule is due
Wire `droid-brain run` into system cron / CI / `droid-brain run --loop`.
"""

from droid_brain.connectors import Connector, EntitySpec


class ExampleConnector(Connector):
    name = "example-connector"

    # mcp_url = "${EXAMPLE_MCP_URL}"                       # from env at run time
    # mcp_auth_headers = {"Authorization": "Bearer ${EXAMPLE_TOKEN}"}
    schedule = "daily"                                     # manual | daily | 6h | 1w

    def extract_notes(self):
        # Replace with self.paginate("<tool>", result_key="items") against mcp_url.
        for i in (1, 2):
            yield EntitySpec(
                doc_type="note",
                id=f"note:auto-{i}",
                name=f"Auto note {i}",
                fields={"description": "Created by the example scheduled connector."},
            )
'''

# Agent skill — the instructions an agent follows when editing this brain.
# Lives at .claude/skills/edit-brain/SKILL.md so Claude Code auto-discovers it.
SKILL_MD = """\
---
name: edit-brain
description: >-
  How to add or edit knowledge in this droid-brain — define doc_types, add or
  update entities, and correlate them with relationships. Use whenever the user
  asks to add/update knowledge, define a concept, record a learning, or link two
  things in the brain.
---

# Editing this droid-brain

This repo is a droid-brain (a context graph). You can edit it two equivalent ways
— both land in the same validated store, so pick whichever fits:

- **Over MCP** (server `droid-brain`): `create_doc_type`, `put_entity`. Preferred
  when the server is connected.
- **By editing files** then running `droid-brain index` and `droid-brain validate`.

## Always start here
Call `navigation_guidelines()` (MCP) or read `doc_types/*.yaml`. It tells you which
doc_types exist, their fields, and the **relationship vocabulary already in use**.
Reuse existing doc_types and relationship meanings instead of inventing near-duplicates.

## Add / update an entity
- Id must be `<doc_type>:<slug>` (e.g. `service:checkout`). `put_entity` is an upsert.
- Fill the doc_type's schema fields. Correlate with `related_to`:
  `{ "target": "<id>", "relationship_edge_meaning": "<meaning>" }`. Prefer a
  **declared** relationship meaning for the doc_type (see the guide); it's validated.
- File-backed doc_types → the entity is written to `entities/<doc_type>/<slug>.json`.
  Index-backed → it lives only in the DB. You don't choose per-write; the doc_type does.

## Define a new doc_type (only if none fits)
`create_doc_type`, or write `doc_types/<name>.yaml`:
```yaml
doc_type: <name>
description: <what it is>
storage: index            # index = DB-owned (generated/high-volume) · file = git-tracked (curated)
display: { label_field: name, color: "#6b7280" }
schema:
  fields:
    - { name: name, type: string, search: syntactic, boost: 6 }   # boost = search weight
    - { name: description, type: text, search: semantic }
relationships:            # declare the edges this type uses, so they're discoverable
  - { name: "<meaning>", target_doc_type: <other_type> }
```

## After editing files
Run `droid-brain index` (loads file-backed entities) then `droid-brain validate`.
Fix any reported errors (bad ids, wrong relationship target types) before finishing.
"""

GITIGNORE = """\
brain.db
brain.db-journal
brain.db-wal
.droid_brain_state.json
"""


def init_brain(brain_dir: Path, name: str) -> None:
    brain_dir.mkdir(parents=True, exist_ok=True)
    (brain_dir / "doc_types").mkdir(exist_ok=True)
    (brain_dir / "entities" / "note").mkdir(parents=True, exist_ok=True)
    (brain_dir / "connectors").mkdir(exist_ok=True)
    (brain_dir / "hooks").mkdir(exist_ok=True)
    skill_dir = brain_dir / ".claude" / "skills" / "edit-brain"
    skill_dir.mkdir(parents=True, exist_ok=True)

    (brain_dir / "brain.yaml").write_text(BRAIN_YAML.format(name=name))
    (brain_dir / "doc_types" / "note.yaml").write_text(EXAMPLE_DOC_TYPE)
    (brain_dir / "entities" / "note" / "welcome.json").write_text(EXAMPLE_ENTITY)

    # Claude Code / agent integration — the brain is usable by an agent immediately.
    (brain_dir / ".mcp.json").write_text(MCP_JSON)
    (brain_dir / "CLAUDE.md").write_text(CLAUDE_MD.format(name=name))
    (skill_dir / "SKILL.md").write_text(SKILL_MD)
    (brain_dir / "connectors" / "example_connector.py").write_text(EXAMPLE_SCHEDULED_CONNECTOR)
    (brain_dir / "hooks" / "reflect.sh").write_text(REFLECT_HOOK)
    (brain_dir / ".gitignore").write_text(GITIGNORE)
