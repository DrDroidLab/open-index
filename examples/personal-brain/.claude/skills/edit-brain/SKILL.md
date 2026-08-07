---
name: edit-brain
description: >-
  How to add or edit knowledge in this brain — define doc_types, add or
  update entities, and correlate them with relationships. Use whenever the user
  asks to add/update knowledge, define a concept, record a learning, or link two
  things in the brain.
---

# Editing this brain

This repo is a **brain** (a context graph built with Open Index). You can edit it two equivalent ways
— both land in the same validated store, so pick whichever fits:

- **Over MCP** (server `open-index`): `create_doc_type`, `put_entity`. Preferred
  when the server is connected.
- **By editing files** then running `open-index index` and `open-index validate`.

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
Run `open-index index` (loads file-backed entities) then `open-index validate`.
Fix any reported errors (bad ids, wrong relationship target types) before finishing.
