---
name: setup-open-index
description: >-
  Install and configure Open Index as the read/write context layer for a
  domain-specialized agent. Use when setting up a legal, marketing, customer
  support, sales, infrastructure, or other domain agent with Open Index over MCP.
---

# Set up Open Index for a domain agent

Open Index gives a domain-specialized agent structured context it can search,
traverse, and maintain. It is read/write by default; use `--read-only` only when
the agent must never update domain knowledge.

## 1. Install

From a local checkout:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[mcp]'
```

If Open Index is published as a package, install the equivalent `open-index[mcp]`
release instead. Never invent or hardcode credentials during setup.

## 2. Initialize the domain brain

Choose a short directory name and describe the actual domain in `brain.yaml`:

```bash
open-index init <brain-name>
cd <brain-name>
open-index validate
open-index index
```

Replace the example `note` schema with concepts meaningful to the domain. For
example, a legal agent might model matters, clauses, obligations, and precedents;
a support agent might model products, issues, customer segments, and resolutions.

## 3. Connect the MCP server

Configure the agent runtime to launch this command from the brain directory:

```bash
open-index mcp --brain /absolute/path/to/<brain-name>
```

Generic MCP configuration shape:

```json
{
  "mcpServers": {
    "open-index": {
      "command": "open-index",
      "args": ["mcp", "--brain", "/absolute/path/to/<brain-name>"]
    }
  }
}
```

Adapt the location of this configuration to the selected client. Claude Code's
optional adapter is `.mcp.json`; OpenClaw and Hermes may install this `SKILL.md`
in their own skills directory while their MCP connection is configured according
to that runtime's current documentation.

For retrieval-only access, append `--read-only`. Do not add it by default.

## 4. Verify the agent connection

Confirm the connected server exposes these default tools:

- `navigation_guidelines`
- `search_brain`
- `get_entity`
- `put_entity`
- `create_doc_type`

The MCP host should inject the server's dynamic domain-context instructions into
the agent prompt. `navigation_guidelines()` is a refresh/fallback for hosts that
do not support server instructions or after the schema/index changes.

Run a safe smoke test using synthetic content:

1. Search the brain and confirm the welcome entity is returned.
2. Create a synthetic entity that matches an existing doc type.
3. Fetch it by ID and verify its fields.
4. Remove it if the setup should remain clean.

In read-only mode, verify `put_entity` and `create_doc_type` are absent.

## 5. Production setup

Use `open-index serve` for a remote MCP endpoint and configure authentication.
Use the OpenSearch backend for shared or multi-writer deployments. Keep secrets in
environment variables, never in `brain.yaml`, skill files, examples, or logs.

## Guardrails

- Treat the brain's domain context according to its sensitivity and access policy.
- Do not put credentials, private keys, tokens, or unrelated personal data in it.
- Reuse existing doc types and relationship meanings before creating new ones.
- Run `open-index validate` after changing file-backed schemas or entities.
- Do not claim setup is complete until tool discovery and a synthetic query pass.
