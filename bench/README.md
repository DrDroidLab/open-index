# droid-brain benchmark harness (`bench/`)

This directory contains the evaluation harness used to compare memory-system
arms (structured brain, flat brain, long-context baseline) on long-context and
update-heavy QA benchmarks. It imports the core `droid_brain` package as a
library and does not change production code.

## Status

Phase 1 (tasks 1–4) is implemented: skeleton/config, dataset fetch/caching,
common IR types, and LongMemEval + MemoryAgentBench adapters. Later phases will
add the LLM agent, memory system arms, runner, scorer, and report generator.

## Prerequisites

- Python 3.12+ (already installed in this environment)
- `droid_brain` installed from the repo root: `pip install -e .`
- Bench-specific packages already installed: `openai`, `datasets`, `pytest`,
  `pandas`, `tiktoken`, `PyYAML`, `requests`
- LLM credentials: the harness expects Azure OpenAI credentials in
  `/code/.secrets/azure-openai.env` (outside the repo, mode 600) as:
  - `AZURE_OPENAI_ENDPOINT` — base URL ending in `/openai/v1`
  - `AZURE_OPENAI_API_KEY`

If the environment variables are already exported, the env file is skipped. The
harness never writes the key into the repository and never logs it. If the env
file is missing, the harness fails early with a clear message (Phase 1 does not
call the LLM, so credentials are only verified on LLM-backed commands in later
phases).

## Dataset licenses

- **LongMemEval-S cleaned** (`xiaowu0162/longmemeval-cleaned`): MIT license.
- **MemoryAgentBench** (`ai-hyz/MemoryAgentBench`): MIT license.

See the Hugging Face dataset cards for full license text and attribution.

## How to run

Fetch and verify the datasets:

```bash
python3 -m bench.data.fetch_eval_assets --verify
```

Run the unit tests:

```bash
python3 -m pytest bench/tests -q
```

(Runner/scorer/report commands will be added in Phase 2.)

## Layout

```
bench/
├── config.py              # paths, model constants, env-file loading, RunConfig
├── README.md              # this file
├── data/
│   ├── fetch_eval_assets.py   # download/cache datasets from Hugging Face
│   ├── longmemeval.py         # LongMemEval-S cleaned adapter
│   └── memoryagentbench.py    # MemoryAgentBench adapter
├── ir/
│   └── types.py               # EvidenceEvent, Question, BenchmarkInstance
├── tests/                 # pytest unit tests + optional integration smoke
└── results/               # generated reports (raw logs are gitignored)
```

## Notes

- `bench/cache/` is gitignored. Downloaded datasets live there and are not
  committed.
- Phase 1 does not make LLM calls. The model constants (`gpt-4o-mini` agent,
  `gpt-4o` judge, temperature 0) are configured now and used by later phases.
