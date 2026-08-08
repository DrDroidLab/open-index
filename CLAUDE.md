# Open Index repository guidance

This is the **open-source Open Index repository**. Everything committed here or
submitted to its GitHub issues, discussions, and comments may become public.

## Public-safety rules

- Never commit or publish PII, customer data, private incident details, internal
  hostnames/URLs, proprietary context, credentials, API keys, tokens, or secrets.
- Use synthetic names and data in source, tests, examples, screenshots, and docs.
- Read credentials only from environment variables; never print or persist them.
- Do not add generated local state (`*.db`, `.open_index_state.json`) to git.
