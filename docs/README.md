# Open Index docs

The documentation site, built with [Mintlify](https://mintlify.com). Content lives
in `.mdx` files; navigation and theme are configured in [`docs.json`](./docs.json).

## Preview locally

```bash
npm i -g mint          # install the Mintlify CLI once
cd docs
mint dev               # serve at http://localhost:3000
```

## Check for broken links

```bash
cd docs
mint broken-links
```

## Structure

| Path | Contents |
|---|---|
| `index.mdx` | Landing page |
| `quickstart.mdx` | Install + first brain |
| `concepts.mdx` | The four primitives |
| `use-cases.mdx` | Example domains |
| `guides/` | Creating a brain, populating entities, entity management, search config |
| `agents/` | MCP context layer, connectors |
| `deployment.mdx` | Local / remote / Docker deployment |
| `reference/cli.mdx` | Every `open-index` command |

Deploy by connecting this repo in the Mintlify dashboard (root directory `docs`);
pushes to the default branch publish automatically.
